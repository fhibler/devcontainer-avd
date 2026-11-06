#!/usr/bin/env python3
"""Prepare portable download inputs, never installed virtual environments."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shlex
import shutil
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("avd_ci", ROOT / "scripts/ci.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)
cache = ci.setup.cache
REPOSITORY = "https://github.com/aristanetworks/avd.git"
WARMUP_IMAGE = "registry.access.redhat.com/ubi9/ubi-minimal:latest"


def work():
    return Path(os.environ.get("AVD_DOWNLOAD_WORK", ROOT / "tmp/downloads"))


def minor(options):
    return ci.scenario_minor(options)


def cases(python, provenance):
    scenarios = json.loads((ROOT / "test/scenarios.json").read_text())
    # Include the platform default even if no option scenario uses it.
    scenarios += [{"options": {"mode": "runtime"}}]
    result = []
    for scenario in scenarios:
        options = dict(scenario["options"])
        if minor(options) != python:
            continue
        options["pythonVersion"] = options.get("pythonVersion", "auto")
        if options.get("mode") == "source":
            options["sourceType"] = "git"
            options["sourceRef"] = provenance[scenario["source"]]
        else:
            options.setdefault("version", provenance["avd"])
        if options not in result:
            result.append(options)
    if not result:
        raise ValueError(f"No dependency cases for Python {python}")
    return result


def sources():
    provenance = json.loads((ci.ARTIFACTS / "provenance.json").read_text())
    shared = work() / "shared"
    destination = shared / "sources"
    destination.mkdir(parents=True, exist_ok=True)
    commits = {provenance["baseline"], provenance["devel"]}
    if any(not re.fullmatch(r"[0-9a-f]{40}", commit) for commit in commits):
        raise ValueError("Source snapshots require full commit identities")
    for path in destination.iterdir():
        if path.stem not in commits:
            path.unlink()
    missing = [commit for commit in sorted(commits) if cache.source(REPOSITORY, commit, directory=shared) is None]
    with tempfile.TemporaryDirectory(prefix="avd-source-inputs-") as directory:
        checkout = Path(directory) / "source"
        if missing:
            ci.download(
                "git",
                "clone",
                "--no-checkout",
                REPOSITORY,
                checkout,
                retry_cleanup=lambda: shutil.rmtree(checkout, ignore_errors=True),
            )
        for commit in missing:
            ci.run("git", "-C", checkout, "checkout", "--detach", commit)
            assert ci.setup.git_details(checkout) == {"commit": commit, "dirty": False}
            archive = destination / (commit + ".tgz")
            with tarfile.open(archive, "w:gz", compresslevel=1) as stream:
                stream.add(checkout, arcname=".")
            cache.write_json(
                destination / (commit + ".json"),
                {"repository": REPOSITORY, "commit": commit, "sha256": cache.sha256(archive)},
            )
    cache.pack(shared, work() / "bundles/shared.tgz")


def warmup_script(selected):
    lines = [
        "set -euo pipefail",
        # Downloads are public inputs. Normalize access for source hooks and
        # return ownership to the runner, including after a partial failure.
        "trap 'chmod -R a+rX /downloads; chown -R \"$AVD_CACHE_UID:$AVD_CACHE_GID\" /downloads' EXIT",
        "used=$(mktemp)",
    ]
    for options in selected:
        env = [
            f"{name.upper()}={str(value).lower() if isinstance(value, bool) else value}"
            for name, value in options.items()
        ]
        lines += [
            # Only disposable warmup-container state is removed between cases.
            "rm -f /opt/avd/config.json /opt/avd/state.json",
            "rm -rf /opt/avd/venv /root/.local/share/avd",
            shlex.join(["env", *env, "bash", "/feature/install.sh"]),
        ]
        if options.get("mode") == "source":
            lines.append("/usr/local/bin/avd-setup-source")
        lines.append('/opt/avd/bootstrap-python /repo/scripts/downloads.py record-python >> "$used"')
        lines.append("/opt/avd/bootstrap-python /repo/scripts/downloads.py index")
    lines += [
        "/opt/avd/bootstrap/uv cache prune --cache-dir /downloads/uv",
        '/opt/avd/bootstrap-python /repo/scripts/downloads.py prune-python "$used"',
    ]
    return "\n".join(lines)


def reset_resolutions(raw):
    # Preparation refreshes metadata; daily Galaxy resolutions can be reused
    # on reruns. Immutable wheels and interpreter archives survive both.
    for name in ("metadata", "sources"):
        shutil.rmtree(raw / name, ignore_errors=True)
    galaxy = raw / "galaxy"
    if galaxy.is_dir():
        for path in galaxy.iterdir():
            manifest = path / "manifest.json"
            if not manifest.is_file() or json.loads(manifest.read_text())["epoch"] != cache.today():
                shutil.rmtree(path)


def warm(python):
    ci.native_arch()
    provenance = json.loads((ci.ARTIFACTS / "provenance.json").read_text())
    raw = work() / "raw" / ("py" + python.replace(".", ""))
    raw.mkdir(parents=True, exist_ok=True)
    reset_resolutions(raw)
    with tempfile.TemporaryDirectory(prefix="avd-inputs-") as directory:
        shared = Path(directory) / "shared"
        cache.unpack(work() / "bundles/shared.tgz", shared)
        shutil.copytree(shared, raw, dirs_exist_ok=True, symlinks=True)
    cache.write_checksums(raw)
    log = ci.ARTIFACTS / ("downloads-py" + python.replace(".", ""))
    log.mkdir(parents=True, exist_ok=True)
    image = ci.identity(WARMUP_IMAGE, log)
    with tempfile.TemporaryDirectory(prefix="avd-warm-feature-") as directory:
        feature = Path(directory) / "feature"
        ci.extract(ci.ARTIFACTS / "devcontainer-feature-avd.tgz", feature)
        ci.run(
            ci.ENGINE,
            "run",
            "--rm",
            "-v",
            f"{feature}:/feature:ro",
            "-v",
            f"{ROOT}:/repo:ro",
            "-v",
            f"{raw}:/downloads",
            "-e",
            "AVD_DOWNLOAD_CACHE=/downloads",
            "-e",
            "AVD_CACHE_RECORD=/downloads",
            "-e",
            f"AVD_CACHE_UID={os.getuid()}",
            "-e",
            f"AVD_CACHE_GID={os.getgid()}",
            "--entrypoint",
            "bash",
            image,
            "-c",
            warmup_script(cases(python, provenance)),
            timeout=60 * 60,
            log=log / "warmup.log",
        )
    # Source archives are shared independently; do not duplicate them per architecture/minor.
    shutil.rmtree(raw / "sources", ignore_errors=True)
    cache.pack(raw, work() / f"bundles/py{python.replace('.', '')}.tgz")


def record_python():
    selected = Path("/opt/avd/bootstrap-python").resolve().parents[1]
    target = Path("/downloads/python") / (selected.name + ".tar.gz")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        temporary = target.with_suffix(".tmp")
        with tarfile.open(temporary, "w:gz", compresslevel=1) as stream:
            stream.add(selected, arcname=selected.name)
        temporary.replace(target)
    print(target.name)


def prune_python(used):
    keep = set(Path(used).read_text().splitlines())
    for path in Path("/downloads/python").iterdir():
        if path.name not in keep:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()


def merge():
    destination = work() / "seed"
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    # Verify every bundle independently before merging overlapping metadata.
    archives = sorted((work() / "bundles").glob("*.tgz"))
    if not archives:
        raise ValueError("No prepared download bundles were provided")
    for archive in archives:
        with tempfile.TemporaryDirectory(prefix="avd-verified-") as directory:
            cache.unpack(archive, Path(directory))
            cache.copy_tree(directory, destination)
    cache.write_checksums(destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("sources", "warm", "merge", "record-python", "prune-python", "index"))
    parser.add_argument("value", nargs="?")
    args = parser.parse_args()
    if args.command == "sources":
        sources()
    elif args.command == "warm":
        warm(args.value)
    elif args.command == "merge":
        merge()
    elif args.command == "record-python":
        record_python()
    elif args.command == "prune-python":
        prune_python(args.value)
    else:
        cache.write_checksums(Path("/downloads"))


if __name__ == "__main__":
    main()
