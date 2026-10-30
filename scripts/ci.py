#!/usr/bin/env python3
"""Package once, then test the extracted production payload in native containers."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
ENGINE = os.environ.get("CONTAINER_ENGINE", "docker")
spec = importlib.util.spec_from_file_location("avd_setup", ROOT / "src/avd/avd_setup.py")
setup = importlib.util.module_from_spec(spec)
sys.dont_write_bytecode = True
spec.loader.exec_module(setup)


def run(*args, **kwargs):
    print("+", setup.redact(" ".join(map(str, args))), flush=True)
    result = subprocess.run(list(map(str, args)), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **kwargs)
    output = result.stdout.strip()
    if result.returncode:
        output = setup.redact(output)
        ARTIFACTS.mkdir(exist_ok=True)
        (ARTIFACTS / "failure.log").write_text(output + "\n")
        print(output, flush=True)
        raise subprocess.CalledProcessError(result.returncode, args, output=output)
    return output


def versions():
    return dict(
        line.split("=", 1)
        for line in (ROOT / "scripts/versions.env").read_text().splitlines()
        if line and not line.startswith("#")
    )


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(setup.sanitize(value), indent=2) + "\n")


def extract(archive, destination):
    with tarfile.open(archive) as handle:
        handle.extractall(destination, filter="data")
    assert (destination / "install.sh").is_file(), "Incomplete packaged Feature"
    for file in (
        "avd_setup.py",
        "bootstrap.env",
        "tooling-requirements.txt",
        "avd-command",
        "devcontainer-feature.json",
    ):
        assert (destination / file).is_file(), f"Missing packaged payload: {file}"


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
    devel = run("git", "ls-remote", "https://github.com/aristanetworks/avd.git", "refs/heads/devel").split()[0]
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
        },
    )
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write(
                "images="
                + json.dumps(json.loads((ROOT / "test/images.json").read_text()), separators=(",", ":"))
                + "\n"
            )
            handle.write(
                "scenarios="
                + json.dumps(json.loads((ROOT / "test/scenarios.json").read_text()), separators=(",", ":"))
                + "\n"
            )
            handle.write(f"version={feature['version']}\ncommit={run('git', 'rev-parse', 'HEAD')}\n")
    with tempfile.TemporaryDirectory() as directory:
        extract(archive, Path(directory))


def native_arch():
    arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}[platform.machine()]
    details = json.loads(run(ENGINE, "info", "--format", "{{json .}}"))
    actual = details.get("Architecture") or details.get("host", {}).get("arch")
    assert actual in (arch, "aarch64" if arch == "arm64" else "x86_64"), (actual, arch)
    return arch


def identity(image, log):
    run(ENGINE, "pull", image)
    details = json.loads(run(ENGINE, "image", "inspect", image))[0]
    write(log / "image.json", {key: details.get(key) for key in ("Id", "RepoDigests", "Architecture", "Os")})
    assert details["Architecture"] == native_arch()
    return details["RepoDigests"][0]


def offline_smoke(container, log):
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
        dockerfile.write_text(
            f"FROM {image_id}\nCOPY feature /tmp/feature\nRUN VERSION={provenance['avd']} bash /tmp/feature/install.sh\nENV PATH=/opt/avd/venv/bin:$PATH\n"
        )
        tag = "avd-platform-" + uuid.uuid4().hex
        container = "avd-platform-" + uuid.uuid4().hex
        try:
            subprocess.run([ENGINE, "build", "-t", tag, "-f", str(dockerfile), str(directory)], check=True)
            run(ENGINE, "create", "--name", container, tag, "sleep", "infinity")
            run(ENGINE, "start", container)
            (log / "manifest.json").write_text(run(ENGINE, "exec", container, "avd-info", "--json"))
            offline_smoke(container, log)
            # A separate disposable snapshot owns bootstrap failure cases.
            if image == "ubuntu:26.10":
                run(
                    ENGINE, "exec", container, "/opt/avd/venv/bin/python", "/tmp/avd-test/support/bootstrap_failures.py"
                )
        finally:
            subprocess.run([ENGINE, "rm", "-f", container], check=False)
            subprocess.run([ENGINE, "rmi", tag], check=False)


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
                'import pyavd; from pathlib import Path; assert Path("/opt/avd/venv/bin/ansible-playbook").exists(); Path("/tmp/consumer-hook").write_text("complete")',
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
    identity(scenario["image"], log)
    # Directory permissions/UID exercise bind mounts independently from build UID.
    workspace = Path(tempfile.mkdtemp(prefix="avd-lifecycle-"))
    container = None
    try:
        source = scenario.get("source")
        if source and scenario["options"].get("sourceType", "workspace") == "workspace":
            run("git", "clone", "--filter=blob:none", "https://github.com/aristanetworks/avd.git", workspace)
            run("git", "-C", workspace, "checkout", "--detach", provenance[source])
        configdir = workspace / ".devcontainer"
        configdir.mkdir(exist_ok=True)
        extract(ARTIFACTS / "devcontainer-feature-avd.tgz", configdir / "avd")
        config = lifecycle_config(scenario, provenance)
        os.environ.update(config["containerEnv"])
        if scenario.get("uidAdjustment"):
            original_uid = int(run(ENGINE, "run", "--rm", scenario["image"], "id", "-u", scenario["user"]))
            assert os.getuid() != original_uid, "UID test requires a host UID different from the image's build UID"
        (configdir / "devcontainer.json").write_text(json.dumps(config, indent=2) + "\n")
        # Real up is essential: invoking the source helper manually cannot prove
        # hook execution, workspace context, UID handling, or consumer ordering.
        output = run(
            "devcontainer", "up", "--docker-path", ENGINE, "--workspace-folder", workspace, "--log-level", "info"
        )
        (log / "up.log").write_text(setup.redact(output))
        result = json.loads(output.splitlines()[-1])
        container = result["containerId"]
        assert exec_container(workspace, "cat", "/tmp/consumer-hook") == "complete"
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
            assert exec_container(workspace, "cat", "/tmp/consumer-hook") == "complete"
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
        shutil.rmtree(workspace)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "platform", "scenario"))
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare()
    elif args.command == "platform":
        platform_smoke(args.value)
    else:
        scenario = next(
            row for row in json.loads((ROOT / "test/scenarios.json").read_text()) if row["name"] == args.value
        )
        lifecycle(scenario)


if __name__ == "__main__":
    main()
