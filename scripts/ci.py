#!/usr/bin/env python3
"""Package once, then test the extracted production payload in native containers."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
ENGINE = os.environ.get("CONTAINER_ENGINE", "docker")
MAX_SCENARIOS_PER_GROUP = 2
PLATFORM_BUILD_TIMEOUT_SECONDS = 20 * 60
LIFECYCLE_TIMEOUT_SECONDS = 30 * 60
IMAGE_PULL_TIMEOUT_SECONDS = 3 * 60
DOWNLOAD_ATTEMPTS = 3
TRANSIENT_PULL_ERROR = re.compile(
    r"timeout|timed out|temporary failure|no such host|connection (?:reset|refused|aborted)|"
    r"network is unreachable|unexpected EOF|TLS handshake|too many requests|"
    r"\b(?:429|500|502|503|504)\b|service unavailable|bad gateway|gateway timeout",
    re.IGNORECASE,
)
ACTIVE_TEST_ARTIFACTS = None
IMAGE_IDENTITIES = {}
# Co-locate tags that commonly resolve to the same image so BuildKit can reuse
# the completed feature-install layer. If a pair's digests diverge, both still run.
PLATFORM_IMAGE_ALIAS_GROUPS = [
    ("debian-13", ("debian:13", "debian:latest")),
    ("debian-13-slim", ("debian:13-slim", "debian:stable-slim")),
    ("ubuntu-26.04", ("ubuntu:26.04", "ubuntu:latest")),
    ("ubuntu-26.10", ("ubuntu:26.10", "ubuntu:rolling")),
    ("almalinux-9", ("almalinux:9", "almalinux:9.8")),
    ("almalinux-10", ("almalinux:10", "almalinux:10.2")),
    ("rockylinux-10", ("quay.io/rockylinux/rockylinux:10", "quay.io/rockylinux/rockylinux:10.2")),
]
spec = importlib.util.spec_from_file_location("avd_setup", ROOT / "src/avd/avd_setup.py")
setup = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
spec.loader.exec_module(setup)


def run(*args, log=None, **kwargs):
    print("+", setup.redact(" ".join(map(str, args))), flush=True)
    try:
        result = subprocess.run(
            list(map(str, args)), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs
        )
    except subprocess.TimeoutExpired as exc:
        if log is not None:
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            log.write_text(setup.redact(output) + "\n")
        raise
    output = result.stdout.strip()
    if log is not None:
        log.write_text(setup.redact(output) + "\n")
    if result.returncode:
        output = setup.redact(output)
        failure_dir = ACTIVE_TEST_ARTIFACTS or ARTIFACTS
        failure_dir.mkdir(parents=True, exist_ok=True)
        (failure_dir / "failure.log").write_text(output + "\n")
        print(output, flush=True)
        raise subprocess.CalledProcessError(result.returncode, args, output=output)
    return output


def versions():
    return dict(
        line.split("=", 1)
        for line in (ROOT / "scripts/versions.env").read_text().splitlines()
        if line and not line.startswith("#")
    )


def download(*args, timeout=setup.DOWNLOAD_TIMEOUT, retry_cleanup=None):
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            return run(*args, timeout=timeout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            if isinstance(exc, subprocess.CalledProcessError) and not setup.transient_download_error(exc.output or ""):
                raise
            if retry_cleanup:
                retry_cleanup()
            setup.download_backoff(Path(args[0]).name, attempt)
    raise AssertionError("unreachable")


def build_cache_key(provenance):
    # Refresh unpinned transitive dependencies daily. Source files, Feature
    # options, Python versions and base-image digests remain BuildKit inputs.
    identity = {key: provenance[key] for key in ("avd", "baseline", "devel", "tools")}
    identity["epoch"] = provenance.get("cache_epoch") or datetime.datetime.now(datetime.UTC).date().isoformat()
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]


@contextlib.contextmanager
def build_cache(name):
    location = os.environ.get("AVD_BUILD_CACHE")
    if not location:
        yield []
        return
    root = Path(location)
    slot = root / hashlib.sha256(name.encode()).hexdigest()[:24]
    pending = slot.with_suffix(".next")
    root.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(pending, ignore_errors=True)
    args = []
    if (slot / "index.json").is_file():
        args += ["--cache-from", f"type=local,src={slot}"]
        print(f"Restoring BuildKit cache for {name}", flush=True)
    else:
        print(f"No saved BuildKit cache for {name}; building normally", flush=True)
    args += ["--cache-to", f"type=local,dest={pending},mode=min"]
    try:
        yield args
        if not (pending / "index.json").is_file():
            raise RuntimeError(f"BuildKit did not export the cache for {name}")
        # Replace the previous export rather than accumulating orphaned blobs
        # across runs. A failed build leaves the restored cache intact.
        shutil.rmtree(slot, ignore_errors=True)
        pending.rename(slot)
    finally:
        shutil.rmtree(pending, ignore_errors=True)


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(setup.sanitize(value), indent=2) + "\n")


def extract(archive, destination):
    with tarfile.open(archive) as handle:
        handle.extractall(destination, filter="data")
    assert (destination / "install.sh").is_file(), "Incomplete packaged Feature"
    for file in (
        "avd_setup.py",
        "avd_cache.py",
        "bootstrap.env",
        "tooling-requirements.txt",
        "avd-command",
        "devcontainer-feature.json",
    ):
        assert (destination / file).is_file(), f"Missing packaged payload: {file}"


def group_scenarios(scenarios):
    by_image = {}
    for scenario in scenarios:
        by_image.setdefault(scenario["image"], []).append(scenario["name"])
    groups = []
    for image, names in by_image.items():
        for offset in range(0, len(names), MAX_SCENARIOS_PER_GROUP):
            group_names = names[offset : offset + MAX_SCENARIOS_PER_GROUP]
            selected = [row for row in scenarios if row["name"] in group_names]
            minors = sorted({scenario_minor(row["options"]) for row in selected})
            groups.append(
                {
                    "id": group_names[0],
                    "image": image,
                    "scenarios": json.dumps(group_names, separators=(",", ":")),
                    "downloads": "py"
                    + (minors[0] if len(minors) == 1 else "3.1[" + "".join(m[-1] for m in minors) + "]"),
                    "sources": any(row.get("source") for row in selected),
                }
            )
    return groups


def scenario_minor(options):
    defaults = {key: value["default"] for key, value in setup.metadata()["options"].items()}
    return ".".join(setup.python_request({**defaults, **options}).split(".")[:2])


def download_seed():
    value = os.environ.get("AVD_CI_DOWNLOADS")
    if not value:
        return None
    path = Path(value)
    setup.cache.verify(path)
    return path


def seed_feature(configdir, config, seed):
    feature = configdir / "avd-downloads"
    feature.mkdir()
    shutil.copytree(seed, feature / "inputs", symlinks=True)
    write(
        feature / "devcontainer-feature.json",
        {
            "id": "avd-downloads",
            "version": "1.0.0",
            "name": "CI download inputs",
            # Buildah may rewrite /tmp permissions when committing Feature
            # bind mounts. Use the standard alternative for installs and hooks.
            "containerEnv": {"TMPDIR": "/var/tmp"},
        },
    )
    installer = feature / "install.sh"
    installer.write_text(
        '#!/bin/sh\nset -eu\ncd "$(dirname "$0")"\n'
        "mkdir -p /opt/avd/download-cache\ncp -a inputs/. /opt/avd/download-cache/\n"
        "mkdir -p /var/tmp\nchmod 1777 /var/tmp\n"
    )
    installer.chmod(0o755)
    config["features"] = {"./avd-downloads": {}, **config["features"]}
    config["overrideFeatureInstallOrder"] = ["./avd-downloads"]


def group_platform_images(images):
    remaining = set(images)
    groups = []
    for group_id, aliases in PLATFORM_IMAGE_ALIAS_GROUPS:
        group_images = [image for image in aliases if image in remaining]
        if len(group_images) > 1:
            groups.append({"id": group_id, "images": json.dumps(group_images, separators=(",", ":"))})
            remaining.difference_update(group_images)
    for image in images:
        if image in remaining:
            groups.append(
                {
                    "id": image.replace("/", "-").replace(":", "-"),
                    "images": json.dumps([image], separators=(",", ":")),
                }
            )
            remaining.remove(image)
    assert not remaining, f"Unassigned platform images: {sorted(remaining)}"
    return groups


def prepare():
    ARTIFACTS.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="avd-package-") as directory:
        staging = Path(directory) / "avd"
        shutil.copytree(ROOT / "src/avd", staging, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copyfile(ROOT / "LICENSE", staging / "LICENSE")
        run("devcontainer", "features", "package", staging, "--output-folder", ARTIFACTS, "--force-clean-output-folder")
    archive = ARTIFACTS / "devcontainer-feature-avd.tgz"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (ARTIFACTS / "SHA256SUMS").write_text(f"{digest}  {archive.name}\n")
    resolved = setup.resolve_release("latest")
    devel = download("git", "ls-remote", "https://github.com/aristanetworks/avd.git", "refs/heads/devel").split()[0]
    feature = json.loads((ROOT / "src/avd/devcontainer-feature.json").read_text())
    write(
        ARTIFACTS / "provenance.json",
        {
            "commit": run("git", "-C", ROOT, "rev-parse", "HEAD"),
            "feature_version": feature["version"],
            "avd": resolved,
            "baseline": versions()["SOURCE_BASELINE"],
            "devel": devel,
            "archive_sha256": digest,
            "tools": versions(),
            "bootstrap": (ROOT / "src/avd/bootstrap.env").read_text(),
            "cache_epoch": datetime.datetime.now(datetime.UTC).date().isoformat(),
        },
    )
    if os.environ.get("GITHUB_OUTPUT"):
        images = json.loads((ROOT / "test/images.json").read_text())
        scenarios = json.loads((ROOT / "test/scenarios.json").read_text())
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write("platform_groups=" + json.dumps(group_platform_images(images), separators=(",", ":")) + "\n")
            handle.write("scenario_groups=" + json.dumps(group_scenarios(scenarios), separators=(",", ":")) + "\n")
            handle.write(f"version={feature['version']}\ncommit={run('git', 'rev-parse', 'HEAD')}\n")
            handle.write(f"cache_key={build_cache_key(json.loads((ARTIFACTS / 'provenance.json').read_text()))}\n")
            handle.write(f"source_key={versions()['SOURCE_BASELINE']}-{devel}\n")
    with tempfile.TemporaryDirectory() as directory:
        extract(archive, Path(directory))


def native_arch():
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine()]
    details = json.loads(run(ENGINE, "info", "--format", "{{json .}}"))
    actual = details.get("Architecture") or details.get("host", {}).get("arch")
    assert actual in (arch, "aarch64" if arch == "arm64" else "x86_64"), (actual, arch)
    return arch


def pull_image(image, log):
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            run(ENGINE, "pull", image, timeout=IMAGE_PULL_TIMEOUT_SECONDS, log=log / f"pull-attempt-{attempt}.log")
            return
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            if isinstance(exc, subprocess.CalledProcessError) and not TRANSIENT_PULL_ERROR.search(exc.output or ""):
                raise
            delay = 2**attempt
            print(f"Image pull failed transiently; retrying in {delay}s ({attempt}/{DOWNLOAD_ATTEMPTS}).", flush=True)
            time.sleep(delay)


def identity(image, log):
    if image not in IMAGE_IDENTITIES:
        pull_image(image, log)
        IMAGE_IDENTITIES[image] = json.loads(run(ENGINE, "image", "inspect", image))[0]
    details = IMAGE_IDENTITIES[image]
    write(log / "image.json", {key: details.get(key) for key in ("Id", "RepoDigests", "Architecture", "Os")})
    assert details["Architecture"] == native_arch()
    return details["RepoDigests"][0]


def offline_smoke(container, log, *, cached_downloads=False):
    run(ENGINE, "cp", ROOT / "test", f"{container}:/tmp/avd-test")
    config = json.loads(run(ENGINE, "exec", container, "cat", "/opt/avd/config.json"))
    uid = run(ENGINE, "exec", container, "id", "-u", config["user"])
    gid = run(ENGINE, "exec", container, "id", "-g", config["user"])
    run(ENGINE, "exec", "--user", "root", container, "chown", "-R", f"{uid}:{gid}", "/tmp/avd-test")
    # Preserve source bind mounts and configured user, while removing external
    # networking. Override the lifecycle container's shell entrypoint explicitly.
    snapshot = "avd-smoke-" + uuid.uuid4().hex
    offline = "avd-offline-" + uuid.uuid4().hex
    run(ENGINE, "commit", container, snapshot)
    try:
        output = run(
            ENGINE,
            "run",
            "--name",
            offline,
            "--network=none",
            "--volumes-from",
            container,
            "--user",
            f"{uid}:{gid}",
            "--env",
            f"HOME={config['user_home']}",
            "--workdir",
            "/tmp/avd-test",
            "--entrypoint",
            "/opt/avd/venv/bin/python",
            snapshot,
            "/tmp/avd-test/support/smoke.py",
            "/tmp/avd-test",
        )
        (log / "smoke.log").write_text(output + "\n")
        print(output)
        samples = log / "generated"
        samples.mkdir(exist_ok=True)
        for name in ("intended", "documentation"):
            run(ENGINE, "cp", f"{offline}:/tmp/avd-test/avd/inventory/{name}", samples / name)
        if cached_downloads:
            output = run(
                ENGINE,
                "run",
                "--rm",
                "--network=none",
                "--entrypoint",
                "/opt/avd/venv/bin/python",
                snapshot,
                "/tmp/avd-test/support/cache_reinstall.py",
                timeout=PLATFORM_BUILD_TIMEOUT_SECONDS,
                log=log / "cache-reinstall.log",
            )
            print(output.splitlines()[-1], flush=True)
    finally:
        subprocess.run([ENGINE, "rm", "-f", offline], check=False)
        run(ENGINE, "rmi", snapshot)


def platform_smoke(image):
    ARTIFACTS.mkdir(exist_ok=True)
    log = ARTIFACTS / ("platform-" + image.replace("/", "_").replace(":", "_"))
    log.mkdir(exist_ok=True)
    image_id = identity(image, log)
    provenance = json.loads((ARTIFACTS / "provenance.json").read_text())
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        payload = directory / "feature"
        extract(ARTIFACTS / "devcontainer-feature-avd.tgz", payload)
        dockerfile = directory / "Dockerfile"
        seed = download_seed()
        seed_copy = ""
        if seed:
            shutil.copytree(seed, directory / "downloads", symlinks=True)
            seed_copy = "COPY downloads /opt/avd/download-cache\n"
        dockerfile.write_text(
            f"FROM {image_id}\nCOPY feature /tmp/feature\n{seed_copy}ARG AVD_CI_DEPENDENCIES\n"
            f'RUN test -n "$AVD_CI_DEPENDENCIES" && VERSION={provenance["avd"]} bash /tmp/feature/install.sh\n'
            "ENV PATH=/opt/avd/venv/bin:$PATH\n"
        )
        tag = "avd-platform-" + uuid.uuid4().hex
        container = "avd-platform-" + uuid.uuid4().hex
        try:
            with build_cache(image_id) as cache_args:
                # Preserve local Docker/Podman builds when persistent caching is
                # disabled; CI uses the Buildx builder selected by the workflow.
                command = [ENGINE, "buildx", "build", "--load"] if cache_args else [ENGINE, "build"]
                output = run(
                    *command,
                    *cache_args,
                    "--build-arg",
                    f"AVD_CI_DEPENDENCIES={build_cache_key(provenance)}",
                    "-t",
                    tag,
                    "-f",
                    dockerfile,
                    directory,
                    timeout=PLATFORM_BUILD_TIMEOUT_SECONDS,
                    log=log / "build.log",
                )
            print(setup.redact(output), flush=True)
            run(ENGINE, "create", "--name", container, tag, "sleep", "infinity")
            run(ENGINE, "start", container)
            (log / "manifest.json").write_text(run(ENGINE, "exec", container, "avd-info", "--json"))
            offline_smoke(
                container,
                log,
                cached_downloads=bool(seed) and image == "registry.access.redhat.com/ubi9/ubi-minimal:latest",
            )
            # A separate disposable snapshot owns bootstrap failure cases.
            if image == "ubuntu:26.10":
                run(
                    ENGINE, "exec", container, "/opt/avd/venv/bin/python", "/tmp/avd-test/support/bootstrap_failures.py"
                )
        finally:
            subprocess.run([ENGINE, "rm", "-f", container], check=False)
            subprocess.run([ENGINE, "rmi", tag], check=False)


def platform_group(images):
    global ACTIVE_TEST_ARTIFACTS
    failures = []
    for image in images:
        ACTIVE_TEST_ARTIFACTS = ARTIFACTS / ("platform-" + image.replace("/", "_").replace(":", "_"))
        ACTIVE_TEST_ARTIFACTS.mkdir(parents=True, exist_ok=True)
        try:
            platform_smoke(image)
        except Exception:
            traceback.print_exc()
            failures.append(image)
        finally:
            ACTIVE_TEST_ARTIFACTS = None
    if failures:
        raise RuntimeError(f"Platform image(s) failed: {', '.join(failures)}")


def exec_container(workspace, *command):
    return run("devcontainer", "exec", "--docker-path", ENGINE, "--workspace-folder", workspace, *command)


def lifecycle_config(scenario, provenance):
    """Build the configuration exercised by real lifecycle runs."""
    source = scenario.get("source")
    options = dict(scenario["options"])
    if options.get("mode", "runtime") != "source":
        options.setdefault("version", provenance["avd"])
    elif options.get("sourceType") == "git":
        options["sourceRef"] = provenance[source]
    features = {"./avd": options}
    if scenario.get("compose"):
        features["ghcr.io/devcontainers/features/python:1"] = {"version": "3.13"}
        features["ghcr.io/devcontainers/features/common-utils:2"] = {}
    config = (
        json.loads((ROOT / "examples" / scenario["example"] / ".devcontainer/devcontainer.json").read_text())
        if scenario.get("example")
        else {}
    )
    config.update(
        {
            "image": scenario["image"],
            "features": features,
            "containerEnv": {
                "AVD_TEST_TOKEN": "avd-test-token-secret",
                "AVD_TEST_URL": "https://user:fake@example.test/path?token=avd-test-query-secret",
            },
            "postCreateCommand": [
                "/opt/avd/venv/bin/python",
                "-c",
                'import pyavd; from pathlib import Path; assert Path("/opt/avd/venv/bin/ansible-playbook").exists(); Path("/var/tmp/consumer-hook").write_text("complete")',
            ],
            "waitFor": "postCreateCommand",
            "customizations": {"vscode": {"settings": {"files.trimTrailingWhitespace": True}}},
        }
    )
    if scenario.get("user"):
        config["remoteUser"] = scenario["user"]
    if scenario.get("uidAdjustment"):
        config["updateRemoteUserUID"] = True
    return config


def lifecycle(scenario):
    native_arch()
    log = ARTIFACTS / scenario["name"]
    log.mkdir(parents=True, exist_ok=True)
    provenance = json.loads((ARTIFACTS / "provenance.json").read_text())
    image_id = identity(scenario["image"], log)
    # Directory permissions/UID exercise bind mounts independently from build UID.
    workspace = Path(tempfile.mkdtemp(prefix="avd-lifecycle-"))
    container = None
    try:
        source = scenario.get("source")
        seed = download_seed()
        if source and scenario["options"].get("sourceType", "workspace") == "workspace":
            repository = "https://github.com/aristanetworks/avd.git"
            archive = setup.cache.source(repository, provenance[source], directory=seed) if seed else None
            if archive:
                setup.cache.extract(archive, workspace)
                assert setup.git_details(workspace) == {"commit": provenance[source], "dirty": False}
                assert run("git", "-C", workspace, "remote", "get-url", "origin") == repository
            else:
                download(
                    "git",
                    "clone",
                    "--filter=blob:none",
                    repository,
                    workspace,
                    retry_cleanup=lambda: shutil.rmtree(workspace, ignore_errors=True),
                )
                run("git", "-C", workspace, "checkout", "--detach", provenance[source])
        configdir = workspace / ".devcontainer"
        configdir.mkdir(exist_ok=True)
        extract(ARTIFACTS / "devcontainer-feature-avd.tgz", configdir / "avd")
        config = lifecycle_config(scenario, provenance)
        if seed:
            seed_feature(configdir, config, seed)
        config["image"] = image_id
        os.environ.update(config["containerEnv"])
        if scenario.get("uidAdjustment"):
            original_uid = int(run(ENGINE, "run", "--rm", scenario["image"], "id", "-u", scenario["user"]))
            assert os.getuid() != original_uid, "UID test requires a host UID different from the image's build UID"
        (configdir / "devcontainer.json").write_text(json.dumps(config, indent=2) + "\n")
        # Real up is essential: invoking the source helper manually cannot prove
        # hook execution, workspace context, UID handling, or consumer ordering.
        with build_cache(scenario["name"]) as cache_args:
            output = run(
                "devcontainer",
                "up",
                "--docker-path",
                ENGINE,
                "--workspace-folder",
                workspace,
                "--log-level",
                "info",
                *cache_args,
                timeout=LIFECYCLE_TIMEOUT_SECONDS,
                log=log / "up.log",
            )
        (log / "up.log").write_text(setup.redact(output))
        result = json.loads(output.splitlines()[-1])
        container = result["containerId"]
        assert exec_container(workspace, "cat", "/var/tmp/consumer-hook") == "complete"
        merged_output = run(
            "devcontainer",
            "read-configuration",
            "--docker-path",
            ENGINE,
            "--workspace-folder",
            workspace,
            "--include-merged-configuration",
        )
        merged = json.loads(merged_output.splitlines()[-1])["mergedConfiguration"]
        settings = {}
        for customization in merged["customizations"]["vscode"]:
            settings.update(customization.get("settings", {}))
        assert settings["files.trimTrailingWhitespace"] is True
        assert settings["python.defaultInterpreterPath"] == "/opt/avd/venv/bin/python"
        manifest = json.loads(exec_container(workspace, "avd-info", "--json"))
        write(log / "manifest.json", manifest)
        if scenario.get("uidAdjustment"):
            assert manifest["current_uid"] == os.getuid(), manifest
        if source:
            assert manifest["source"]["commit"] == provenance[source], manifest["source"]
            if source == "baseline":
                assert manifest["collections"]["arista.avd"] == "6.4.0", manifest["collections"]
            root = manifest["source"]["root"]
            run(ENGINE, "cp", ROOT / "test", f"{container}:/tmp/avd-test")
            (log / "source-checks.log").write_text(
                setup.redact(
                    exec_container(workspace, "/opt/avd/venv/bin/python", "/tmp/avd-test/lifecycle/source.py", root)
                )
            )
            # Rerun production initialization, preserving dirty tracked files.
            before = exec_container(workspace, "git", "-C", root, "diff")
            exec_container(workspace, "avd-setup-source")
            assert exec_container(workspace, "git", "-C", root, "diff") == before
            exec_container(workspace, "avd-setup-source")
            (log / "source-failures.log").write_text(
                setup.redact(
                    exec_container(workspace, "/opt/avd/venv/bin/python", "/tmp/avd-test/lifecycle/failures.py")
                )
            )
            state_file = manifest["development_home"] + "/.local/share/avd/state.json"
            before_restart = exec_container(workspace, "sha256sum", state_file)
            run(ENGINE, "stop", container)
            run(ENGINE, "start", container)
            assert exec_container(workspace, "sha256sum", state_file) == before_restart
            assert exec_container(workspace, "cat", "/var/tmp/consumer-hook") == "complete"
        if scenario.get("reinstall"):
            run(ENGINE, "cp", configdir / "avd", f"{container}:/tmp/reinstall-feature")
            install_env = [
                f"{name.upper()}={str(value).lower() if isinstance(value, bool) else value}"
                for name, value in config["features"]["./avd"].items()
            ]
            run(
                ENGINE,
                "exec",
                "--user",
                "root",
                container,
                "env",
                *install_env,
                "bash",
                "/tmp/reinstall-feature/install.sh",
            )
            conflict = subprocess.run(
                [
                    ENGINE,
                    "exec",
                    "--user",
                    "root",
                    container,
                    "env",
                    *install_env,
                    "MODE=tools",
                    "bash",
                    "/tmp/reinstall-feature/install.sh",
                ],
                text=True,
                capture_output=True,
            )
            assert conflict.returncode != 0 and "Contradictory duplicate" in conflict.stdout + conflict.stderr
        offline_smoke(container, log)
    finally:
        if container:
            subprocess.run([ENGINE, "rm", "-f", container], check=False)
        else:
            orphan = run(ENGINE, "ps", "-aq", "--filter", f"label=devcontainer.local_folder={workspace}")
            if orphan:
                run(ENGINE, "rm", "-f", *orphan.splitlines())
        shutil.rmtree(workspace, ignore_errors=True)


def lifecycle_group(names):
    global ACTIVE_TEST_ARTIFACTS
    scenarios = {row["name"]: row for row in json.loads((ROOT / "test/scenarios.json").read_text())}
    unknown = [name for name in names if name not in scenarios]
    if unknown:
        raise ValueError(f"Unknown integration scenario(s): {', '.join(unknown)}")

    failures = []
    for name in names:
        ACTIVE_TEST_ARTIFACTS = ARTIFACTS / name
        ACTIVE_TEST_ARTIFACTS.mkdir(parents=True, exist_ok=True)
        try:
            lifecycle(scenarios[name])
        except Exception:
            traceback.print_exc()
            failures.append(name)
        finally:
            ACTIVE_TEST_ARTIFACTS = None
    if failures:
        raise RuntimeError(f"Integration scenario(s) failed: {', '.join(failures)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "platform", "platform-group", "scenario", "scenario-group"))
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "platform":
        platform_smoke(args.value)
    elif args.command == "platform-group":
        platform_group(json.loads(args.value))
    elif args.command == "scenario":
        scenario = next(
            row for row in json.loads((ROOT / "test/scenarios.json").read_text()) if row["name"] == args.value
        )
        lifecycle(scenario)
    else:
        lifecycle_group(json.loads(args.value))


if __name__ == "__main__":
    main()
