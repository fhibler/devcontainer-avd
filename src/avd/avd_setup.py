#!/usr/bin/env python3
"""AVD Feature setup, current-environment probing, and source lifecycle."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PREFIX = Path("/opt/avd")
PAYLOAD = Path(__file__).resolve().parent
UV = PREFIX / "bootstrap/uv"
VENV = PREFIX / "venv"
CONFIG = PREFIX / "config.json"
PYPI = "https://pypi.org/pypi/pyavd/json"
GALAXY = "https://galaxy.ansible.com"
STABLE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
TOOLS = ("ansible-lint", "yamllint", "ruff", "pre-commit", "pytest", "tox", "molecule", "molecule-plugins")


class SetupError(Exception):
    """An actionable setup or verification failure."""


def redact(text):
    """Remove credentials and ambient secrets from displayed diagnostics."""

    def url(match):
        raw = match.group()
        try:
            parts = urllib.parse.urlsplit(raw)
            host = parts.hostname or ""
            if parts.port:
                host += f":{parts.port}"
            return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))
        except ValueError:
            return "[redacted-url]"

    text = re.sub(r"(?:https?|ssh)://[^\s\x22\x27<>]+", url, str(text))
    for key, value in os.environ.items():
        if re.search(r"token|password|secret|authorization|proxy", key, re.I) and len(value) >= 4:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer|basic)?\s*[^\s,;]+", r"\1[redacted]", text)
    return text


def sanitize(value):
    """Redact values before JSON encoding, preserving valid escaping/structure."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {redact(key): sanitize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    return value


def emit(value):
    print(redact(value), flush=True)


def read_json(path):
    return json.loads(Path(path).read_text())


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic metadata replacement only; package installations are in-place.
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o644)
    temporary.replace(path)


def run(args, *, cwd=None, env=None, capture=False, timeout=1800):
    command_env = os.environ.copy()
    command_env.update(env or {})
    try:
        result = subprocess.run(
            [str(arg) for arg in args],
            cwd=cwd,
            env=command_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SetupError(f"Could not execute {Path(args[0]).name}: {redact(exc)}") from None
    if not capture:
        emit(result.stdout.rstrip())
    if result.returncode:
        raise SetupError(
            f"{Path(args[0]).name} failed (exit {result.returncode}). "
            + (redact(result.stdout.strip()) if capture else "See sanitized output above.")
        )
    return result.stdout.strip()


def metadata():
    return read_json(PAYLOAD / "devcontainer-feature.json")


def options_from_env(environ):
    options = {}
    definitions = metadata()["options"]
    for name, definition in definitions.items():
        value = environ.get(name.upper(), definition["default"])
        if definition["type"] == "boolean":
            if isinstance(value, str) and value in ("true", "false"):
                value = value == "true"
            if not isinstance(value, bool):
                raise SetupError(f"{name} must be true or false.")
        elif not isinstance(value, str) or not value or "\x00" in value or "\n" in value or "\r" in value:
            raise SetupError(f"{name} must be a nonempty single-line string.")
        if "enum" in definition and value not in definition["enum"]:
            raise SetupError(f"{name} must be one of {', '.join(definition['enum'])}.")
        options[name] = value
    mode = options["mode"]
    inactive = []
    if mode != "source":
        inactive = ["sourceType", "sourcePath", "sourceRepository", "sourceRef"]
    else:
        inactive = ["version"]
        inactive += ["sourceRepository", "sourceRef"] if options["sourceType"] == "workspace" else ["sourcePath"]
    for name in inactive:
        if options[name] != definitions[name]["default"]:
            raise SetupError(f"{name} is inactive for this mode; leave it at its default.")
    if options["version"] != "latest" and not STABLE.fullmatch(options["version"]):
        raise SetupError("version must be an exact stable X.Y.Z release or latest.")
    if options["version"] != "latest":
        supported_release(options["version"])
    python = options["pythonVersion"]
    if python != "auto" and not re.fullmatch(r"3\.(12|13|14)(\.(0|[1-9][0-9]*))?", python):
        raise SetupError("pythonVersion supports 3.12–3.14, as a minor or exact patch.")
    if mode == "source" and python.startswith("3.14"):
        raise SetupError("Source mode supports Python 3.12–3.13; use auto for the 3.12 baseline.")
    validate_repository(options["sourceRepository"])
    if options["sourceRef"].startswith("-") or re.search(r"[\s~^:?*\[\\]", options["sourceRef"]):
        raise SetupError("sourceRef must be a branch, tag, or commit, without Git revision operators.")
    return options


def validate_repository(repository):
    if re.search(r"\s", repository):
        raise SetupError("sourceRepository must not contain whitespace.")
    try:
        parts = urllib.parse.urlsplit(repository)
        host = parts.hostname
        _ = parts.port  # Validate malformed/non-numeric ports before invoking Git.
    except ValueError:
        raise SetupError("sourceRepository must be a valid HTTPS or SSH Git location.") from None
    if parts.scheme in ("https", "ssh"):
        if (
            not host
            or parts.password is not None
            or parts.query
            or parts.fragment
            or (parts.scheme == "https" and parts.username is not None)
        ):
            raise SetupError("sourceRepository must not contain credentials, query strings, or fragments.")
        return
    if re.fullmatch(r"[\w.-]+@[\w.-]+:[^\s?#]+", repository):
        return
    raise SetupError("sourceRepository must be an HTTPS or SSH Git location without embedded credentials.")


def python_request(options):
    if options["pythonVersion"] != "auto":
        return options["pythonVersion"]
    return "3.12" if options["mode"] == "source" else "3.14"


def stable_key(value):
    return tuple(map(int, value.split("."))) if STABLE.fullmatch(value) else None


def supported_release(version):
    key = stable_key(version)
    if key is None or key < (6, 4, 0) or key >= (7, 0, 0):
        raise SetupError(
            f"AVD {version} is unsupported. This Feature supports >=6.4.0,<7.0.0; update the Feature for another family."
        )


def select_release(request, pypi, galaxy):
    try:
        releases = pypi["releases"]
        python_versions = {
            version
            for version, files in releases.items()
            if stable_key(version) and any(not file["yanked"] for file in files)
        }
        galaxy_versions = {row["version"] for row in galaxy if stable_key(row["version"])}
    except (KeyError, TypeError, AttributeError) as exc:
        raise SetupError("Malformed PyPI/Galaxy release metadata.") from exc
    common = python_versions & galaxy_versions
    if request == "latest":
        if not common:
            raise SetupError("No stable, non-yanked release exists on both PyPI and Galaxy.")
        chosen = max(common, key=stable_key)
    elif request in common:
        chosen = request
    else:
        raise SetupError(f"AVD {request} is not available as a non-yanked stable release on both services.")
    supported_release(chosen)
    return chosen


def http_json(url):
    # No custom authentication storage. urllib honors standard proxy facilities.
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "avd-devcontainer-feature/1.0"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise SetupError(f"Metadata download failed: {redact(url)} (HTTP {exc.code}).") from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise SetupError(f"Metadata download failed: {redact(url)}.") from None
        except (ValueError, TypeError):
            raise SetupError(f"Malformed JSON metadata: {redact(url)}.") from None
        time.sleep(attempt + 1)
    raise AssertionError("unreachable")


def galaxy_versions(namespace="arista", name="avd"):
    url = f"{GALAXY}/api/v3/plugin/ansible/content/published/collections/index/{namespace}/{name}/versions/?limit=100"
    rows = []
    seen = set()
    while url:
        if url in seen or urllib.parse.urlsplit(url).hostname != "galaxy.ansible.com":
            raise SetupError("Invalid Galaxy pagination link.")
        seen.add(url)
        page = http_json(url)
        try:
            rows.extend(page["data"])
            url = urllib.parse.urljoin(GALAXY, page["links"]["next"]) if page["links"]["next"] else None
        except (KeyError, TypeError):
            raise SetupError("Malformed Galaxy pagination metadata.") from None
    return rows


def resolve_release(request):
    return select_release(request, http_json(PYPI), galaxy_versions())


def managed_env(config):
    cache_root = Path(config["user_home"]) / ".local/share/avd" if config["options"]["mode"] == "source" else PREFIX
    return {
        "UV_CACHE_DIR": str(cache_root / "cache/uv"),
        "UV_PYTHON_INSTALL_DIR": str(PREFIX / "python"),
        "UV_PYTHON_DOWNLOADS": "never",
        "UV_PYTHON_PREFERENCE": "only-managed",
    }


def pip_install(config, requirements, *, source=None):
    args = [UV, "pip", "install", "--python", VENV / "bin/python", "--only-binary", ":all:"]
    args += requirements
    if source:
        # Allow the one intentional local editable build; keep every downloaded
        # dependency on published wheels, including build dependencies.
        args += ["--no-binary", "pyavd"]
        args += ["--editable", f"{source / 'python-avd'}[ansible]", "--config-settings", "editable_mode=compat"]
        for group in ("dev", "doc", "coverage"):
            args += ["--group", f"{source / 'pyproject.toml'}:{group}"]
    if config["options"]["installSshSupport"]:
        args += ["paramiko", "ansible-pylibssh"]
    run(args, env=managed_env(config))


def venv_probe(*args):
    return json.loads(run([VENV / "bin/python", PAYLOAD / "avd_setup.py", "probe", *args], capture=True))


def install_collections(requirements, *, source=None):
    destination = venv_probe("--paths")["site_packages"]
    # A command-local configuration prevents project config changing the install
    # destination or servers. No configuration is persisted into the project.
    with tempfile.TemporaryDirectory(prefix="avd-galaxy-") as directory:
        directory = Path(directory)
        config = directory / "ansible.cfg"
        config.write_text("[galaxy]\nserver_list = galaxy\n[galaxy_server.galaxy]\nurl = https://galaxy.ansible.com/\n")
        req = directory / "requirements.json"
        req.write_text(
            json.dumps({"collections": [{"name": name, "version": version} for name, version in requirements.items()]})
        )
        if requirements:
            run(
                [
                    VENV / "bin/ansible-galaxy",
                    "collection",
                    "install",
                    "--force-with-deps",
                    "-r",
                    req,
                    "-p",
                    destination,
                ],
                cwd=directory,
                env={"ANSIBLE_CONFIG": str(config), "ANSIBLE_GALAXY_COLLECTIONS_PATH_WARNING": "false"},
            )
    pth = Path(destination) / "avd-source.pth"
    if source:
        pth.write_text(str(source) + "\n")
    else:
        pth.unlink(missing_ok=True)


def optional_collection():
    controller = venv_probe("--paths")["ansible"]
    rows = [row for row in galaxy_versions("community", "general") if stable_key(row["version"])]
    for row in sorted(rows, key=lambda row: stable_key(row["version"]), reverse=True):
        if venv_probe("--specifier", row.get("requires_ansible") or "*", controller)["compatible"]:
            return row["version"]
    raise SetupError(f"No community.general release is compatible with Ansible {controller}.")


def source_metadata(source):
    try:
        root = tomllib.loads((source / "pyproject.toml").read_text())
        project = tomllib.loads((source / "python-avd/pyproject.toml").read_text())
    except (OSError, ValueError) as exc:
        raise SetupError(f"Invalid AVD source metadata at {source}: {exc}") from None
    if not isinstance(project.get("project"), dict) or project["project"].get("name") != "pyavd":
        raise SetupError("Source python-avd project must declare name = pyavd.")
    groups = root.get("dependency-groups", {})
    if not isinstance(groups, dict) or any(
        not isinstance(groups.get(group), list) for group in ("dev", "doc", "coverage")
    ):
        raise SetupError("Source must declare dev, doc, and coverage dependency groups.")
    collection = source / "ansible_collections/arista/avd"
    if not (collection / "galaxy.yml").is_file() or not (collection / "plugins/__init__.py").is_file():
        raise SetupError("Source must contain the real ansible_collections/arista/avd tree.")
    return project


def source_collection_metadata(source):
    import yaml

    try:
        data = yaml.safe_load((source / "ansible_collections/arista/avd/galaxy.yml").read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise SetupError(f"Malformed source galaxy.yml: {exc}") from None
    if (
        not isinstance(data, dict)
        or data.get("namespace") != "arista"
        or data.get("name") != "avd"
        or not isinstance(data.get("version"), str)
        or not isinstance(data.get("dependencies"), dict)
        or any(
            not isinstance(name, str) or not isinstance(spec, str)
            for name, spec in data.get("dependencies", {}).items()
        )
    ):
        raise SetupError(
            "Malformed source galaxy.yml: expected arista.avd, a string version, and a dependency mapping."
        )
    return data


def git_details(source):
    return {
        "commit": run(["git", "-C", source, "rev-parse", "HEAD"], capture=True),
        "dirty": bool(run(["git", "-C", source, "status", "--porcelain"], capture=True)),
    }


def checkout(config):
    options = config["options"]
    if options["sourceType"] == "workspace":
        return Path(options["sourcePath"]).resolve()
    source = Path(config["user_home"]) / ".local/share/avd/source"
    repository = options["sourceRepository"]
    ref = options["sourceRef"]
    fresh = not source.exists()
    source.parent.mkdir(parents=True, exist_ok=True)
    # Publish a new checkout only after its requested ref has been checked out.
    # A failed clone/fetch leaves no partial worktree to poison the next run.
    staging = tempfile.TemporaryDirectory(prefix=".source-", dir=source.parent) if fresh else contextlib.nullcontext()
    with staging as directory:
        worktree = Path(directory) / "source" if fresh else source
        if fresh:
            run(["git", "clone", "--no-checkout", "--", repository, worktree])
        if run(["git", "-C", worktree, "config", "--get", "remote.origin.url"], capture=True) != repository:
            raise SetupError(f"{source} has a different origin. Move it aside explicitly and rerun avd-setup-source.")
        run(["git", "-C", worktree, "fetch", "--no-tags", "origin", ref])
        target = run(["git", "-C", worktree, "rev-parse", "FETCH_HEAD^{commit}"], capture=True)
        current = None if fresh else run(["git", "-C", worktree, "rev-parse", "HEAD"], capture=True)
        if current != target:
            if not fresh and run(["git", "-C", worktree, "status", "--porcelain"], capture=True):
                raise SetupError(
                    f"{source} contains user changes. Commit/stash them or move the checkout aside, then rerun avd-setup-source. No reset/clean was performed."
                )
            run(["git", "-C", worktree, "checkout", "--detach", target])
        if fresh:
            worktree.rename(source)
    return source.resolve()


def fingerprints(source):
    names = (
        "pyproject.toml",
        "python-avd/pyproject.toml",
        "ansible_collections/arista/avd/galaxy.yml",
        "ansible_collections/arista/avd/meta/runtime.yml",
        "python-avd/scripts/custom_build_backend.py",
    )
    return {
        name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in names if (source / name).is_file()
    }


def environment(config, source=None):
    python = config["python_path"]
    if not (VENV / "bin/python").exists():
        run([UV, "venv", "--python", python, VENV.resolve()], env=managed_env(config))
    actual = run([VENV / "bin/python", "-c", "import platform; print(platform.python_version())"], capture=True)
    if actual != config["python_version"]:
        raise SetupError(
            f"Existing environment uses Python {actual}, requested {config['python_version']}. Rebuild the container."
        )
    if source:
        source_metadata(source)
        pip_install(config, ["anta[cli]"], source=source)
        deps = venv_probe("--source-metadata", str(source))["dependencies"]
    else:
        release = resolve_release(config["options"]["version"])
        config["resolved_avd"] = release
        requirements = [f"pyavd[ansible]=={release}", "anta[cli]"]
        if config["options"]["mode"] == "tools":
            requirements += ["-r", PAYLOAD / "tooling-requirements.txt"]
        pip_install(config, requirements)
        deps = {"arista.avd": release}
    if config["options"]["installCommunityGeneral"]:
        deps["community.general"] = optional_collection()
    install_collections(deps, source=source)
    run([UV, "pip", "check", "--python", VENV / "bin/python"], env=managed_env(config))
    report = verify(config, source)
    return report


def validate_environment(config, report, source=None):
    if report["errors"]:
        raise SetupError("Environment verification failed: " + "; ".join(report["errors"]))
    if report["python_version"] != config["python_version"]:
        raise SetupError("The active interpreter does not match the managed interpreter selection.")
    expected = (
        source / "ansible_collections/arista/avd"
        if source
        else Path(report["site_packages"]) / "ansible_collections/arista/avd"
    )
    if Path(report["collection_path"]).resolve() != expected.resolve():
        raise SetupError(
            f"AVD is shadowed by {report['collection_path']}; expected {expected}. Check project collection paths."
        )
    if source:
        if (
            not Path(report["pyavd_path"]).is_relative_to(source / "python-avd")
            or not report["running_from_source"]
            or not report["pyavd_editable"]
            or report["pyavd_editable_root"] != str(source / "python-avd")
        ):
            raise SetupError(
                "Editable PyAVD and the active AVD collection must originate from the same source checkout with source detection enabled."
            )
    elif (
        report["packages"]["pyavd"] != config["resolved_avd"] or report["collection_version"] != config["resolved_avd"]
    ):
        raise SetupError("Released PyAVD and arista.avd versions must exactly match the selected release.")
    if not source and not Path(report["pyavd_path"]).is_relative_to(Path(report["site_packages"]) / "pyavd"):
        raise SetupError(
            "Released PyAVD is shadowed by a copy outside the Feature environment. Check PYTHONPATH and project configuration."
        )
    for name in TOOLS if config["options"]["mode"] == "tools" else ():
        if name not in report["packages"]:
            raise SetupError(f"Missing tools-mode package: {name}")
    for name in ("paramiko", "ansible-pylibssh") if config["options"]["installSshSupport"] else ():
        if name not in report["packages"]:
            raise SetupError(f"Missing SSH transport: {name}")
    if config["options"]["installCommunityGeneral"] and "community.general" not in report["collections"]:
        raise SetupError("Missing optional collection: community.general")


def verify(config, source=None):
    report = venv_probe()
    validate_environment(config, report, source)
    for command in ("ansible-playbook", "ansible-galaxy", "ansible-doc"):
        run([VENV / "bin" / command, "--version"], capture=True)
    run([VENV / "bin/anta", "--help"], capture=True)
    return report


def probe(args):
    """Runs only through the environment's own interpreter; never system Python."""
    if args.source_metadata:
        return source_collection_metadata(Path(args.source_metadata))
    import importlib
    import importlib.metadata as md
    import platform
    import sysconfig

    import yaml
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version

    site_packages = sysconfig.get_paths()["purelib"]
    controller = md.version("ansible-core")
    if args.specifier:
        specifier, version = args.specifier
        return {"compatible": specifier == "*" or Version(version) in SpecifierSet(specifier)}
    if args.paths:
        return {"site_packages": site_packages, "ansible": controller}
    from ansible import constants as C
    from ansible.plugins.loader import init_plugin_loader
    from ansible.utils.collection_loader import AnsibleCollectionConfig

    init_plugin_loader()
    # Match ansible-playbook's adjacent ./collections discovery for this project.
    AnsibleCollectionConfig.playbook_paths = [str(Path.cwd())]
    errors = []
    if not C.COLLECTIONS_SCAN_SYS_PATH:
        errors.append("Ansible Python-path collection scanning is disabled; enable collections_scan_sys_path")
    packages = {dist.metadata["Name"].lower().replace("_", "-"): dist.version for dist in md.distributions()}
    collections = {}
    collection_paths = {}
    # Probe the loader, so local project configuration and shadowing are visible.
    installed_roots = [Path(site_packages), *map(Path, C.COLLECTIONS_PATHS), Path.cwd() / "collections"]
    source_pth = Path(site_packages) / "avd-source.pth"
    if source_pth.exists():
        installed_roots += [Path(source_pth.read_text().strip())]
    for root in installed_roots:
        for path in sorted((root / "ansible_collections").glob("*/*")):
            if not path.is_dir() or path.name.startswith("_"):
                continue
            name = f"{path.parent.name}.{path.name}"
            try:
                module = importlib.import_module(f"ansible_collections.{name}")
                actual = Path(next(iter(module.__path__))).resolve()
                manifest = actual / "MANIFEST.json"
                info = (
                    read_json(manifest)["collection_info"]
                    if manifest.exists()
                    else yaml.safe_load((actual / "galaxy.yml").read_text())
                )
                collections[name] = str(info["version"])
                collection_paths[name] = str(actual)
                runtime = actual / "meta/runtime.yml"
                requirement = (
                    (yaml.safe_load(runtime.read_text()) or {}).get("requires_ansible", "*")
                    if runtime.exists()
                    else "*"
                )
                if requirement != "*" and Version(controller) not in SpecifierSet(requirement):
                    errors.append(f"{name} requires Ansible {requirement}; installed {controller}")
                for dep, spec in info.get("dependencies", {}).items():
                    dep_module = importlib.import_module(f"ansible_collections.{dep}")
                    dep_path = Path(next(iter(dep_module.__path__)))
                    dep_info = read_json(dep_path / "MANIFEST.json")["collection_info"]
                    if spec != "*" and Version(dep_info["version"]) not in SpecifierSet(spec):
                        errors.append(f"{name} needs {dep}{spec}")
            except (ImportError, OSError, ValueError, KeyError, TypeError, AttributeError, yaml.YAMLError) as exc:
                errors.append(f"Cannot validate {name}: {exc}")
    for dependency in md.requires("pyavd") or []:
        requirement = Requirement(dependency)
        if (
            requirement.marker
            and not requirement.marker.evaluate({"extra": "ansible"})
            and not requirement.marker.evaluate({"extra": "ansible-collection"})
        ):
            continue
        try:
            if md.version(requirement.name) not in requirement.specifier:
                errors.append(f"Unsatisfied PyAVD requirement {requirement}")
        except md.PackageNotFoundError:
            errors.append(f"Missing PyAVD requirement {requirement}")
    direct_url_text = md.distribution("pyavd").read_text("direct_url.json")
    direct_url = json.loads(direct_url_text) if direct_url_text else {}
    editable = direct_url.get("dir_info", {}).get("editable") is True
    origin = urllib.parse.urlsplit(direct_url.get("url", ""))
    editable_root = (
        str(Path(urllib.parse.unquote(origin.path)).resolve()) if editable and origin.scheme == "file" else None
    )
    pyavd = importlib.import_module("pyavd")
    plugins = importlib.import_module("ansible_collections.arista.avd.plugins")
    for module in ("anta.cli", "pyavd._anta", "pyavd._cv.client", "pyavd_utils"):
        importlib.import_module(module)
    if "ansible-pylibssh" in packages:
        importlib.import_module("pylibsshext.session")
        importlib.import_module("paramiko")
    return {
        "python_version": platform.python_version(),
        "interpreter": sys.executable,
        "base_interpreter": sys._base_executable,
        "site_packages": site_packages,
        "packages": packages,
        "collections": collections,
        "collection_paths": collection_paths,
        "collection_path": collection_paths.get("arista.avd", ""),
        "collection_version": collections.get("arista.avd"),
        "pyavd_path": str(Path(pyavd.__file__).resolve()),
        "pyavd_editable": editable,
        "pyavd_editable_root": editable_root,
        "running_from_source": bool(plugins.RUNNING_FROM_SOURCE),
        "ansible_config": str(C.CONFIG_FILE) if C.CONFIG_FILE else None,
        "collection_scan_sys_path": C.COLLECTIONS_SCAN_SYS_PATH,
        "collection_search_paths": list(C.COLLECTIONS_PATHS),
        "errors": errors,
    }


def state_path(config):
    return (
        Path(config["user_home"]) / ".local/share/avd/state.json"
        if config["options"]["mode"] == "source"
        else PREFIX / "state.json"
    )


def context(config):
    import platform

    os_release = platform.freedesktop_os_release()
    definitions = metadata()["options"]
    options = config["options"].copy()
    if options["mode"] != "source":
        inactive = ("sourceType", "sourcePath", "sourceRepository", "sourceRef")
    else:
        inactive = (
            ("version", "sourceRepository", "sourceRef")
            if options["sourceType"] == "workspace"
            else ("version", "sourcePath")
        )
    for key in inactive:
        options[key] = {"status": "inactive", "default": definitions[key]["default"]}
    raw_architecture = platform.machine()
    libc_name, libc_version = platform.libc_ver()
    return {
        "feature_id": metadata()["id"],
        "feature_version": metadata()["version"],
        "phase": "inspection",
        "status": "preparing",
        "mode": options["mode"],
        "options": options,
        "os": {key: os_release.get(key, "") for key in ("ID", "VERSION_ID", "PRETTY_NAME")},
        "architecture": {"x86_64": "amd64", "aarch64": "arm64"}.get(raw_architecture, raw_architecture),
        "raw_architecture": raw_architecture,
        "libc": {"name": libc_name or None, "version": libc_version or None},
        "host_kernel": platform.release(),
        "package_manager": next((name for name in ("apt-get", "dnf", "microdnf") if shutil.which(name)), None),
        "install_user": config["install_user"],
        "development_user": config["user"],
        "development_home": config["user_home"],
        "current_uid": os.getuid(),
        "current_gid": os.getgid(),
        "venv": str(VENV),
        "managed_python": config["python_path"],
        "source_activation": "deferred until post-create" if config["options"]["mode"] == "source" else "inactive",
        "resolved_avd": config.get("resolved_avd", "inactive" if config["options"]["mode"] == "source" else "pending"),
        "requested_python": config["options"]["pythonVersion"],
        "resolved_python": config["python_version"],
        "requested_avd": config["options"]["version"] if config["options"]["mode"] != "source" else "inactive",
        "bootstrap": (PAYLOAD / "bootstrap.env").read_text().splitlines()[1:],
    }


def complete(config, report, source=None):
    report = {
        **context(config),
        **report,
        "phase": "verification",
        "status": "ready",
        "completed_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    if source:
        report["source_activation"] = "complete"
        report["source"] = {"root": str(source), **git_details(source), "fingerprints": fingerprints(source)}
    # Inspection consumes this identity, so paths and versions must stay exact.
    # Successful reports contain no authentication data; display still redacts.
    save_json(state_path(config), report)
    display(report)
    return report


def display(report, as_json=False):
    if as_json:
        print(json.dumps(sanitize(report), indent=2, sort_keys=True), flush=True)
        return
    emit(f"AVD Feature {report['feature_version']}: {report['status']}")
    emit("This Dev Container Feature is not officially supported by Arista Networks.")
    emit("Please open issues at https://github.com/fhibler/devcontainer-avd/issues.")
    for key in (
        "os",
        "phase",
        "architecture",
        "raw_architecture",
        "libc",
        "host_kernel",
        "package_manager",
        "options",
        "install_user",
        "development_user",
        "development_home",
        "current_uid",
        "current_gid",
        "managed_python",
        "resolved_avd",
        "requested_python",
        "resolved_python",
        "interpreter",
        "venv",
        "ansible_config",
        "collection_search_paths",
        "source",
        "errors",
    ):
        if key in report:
            emit(f"  {key}: {json.dumps(report[key], sort_keys=True)}")
    if "packages" in report:
        emit("  packages: " + json.dumps(report["packages"], sort_keys=True))
        emit("  collections: " + json.dumps(report["collections"], sort_keys=True))


def install():
    options = options_from_env(os.environ)
    if CONFIG.exists():
        previous = read_json(CONFIG)
        if previous["options"] != options:
            raise SetupError(
                "Contradictory duplicate Feature installation. Use one option set and rebuild the container."
            )
    user = os.environ.get("_REMOTE_USER", "root")
    try:
        account = pwd.getpwuid(int(user)) if user.isdecimal() else pwd.getpwnam(user)
        user = account.pw_name
    except KeyError:
        raise SetupError(f"Configured remote user {user!r} does not exist. Create it before this Feature.") from None
    home = Path(os.environ.get("_REMOTE_USER_HOME", account.pw_dir))
    if not home.is_absolute() or home != Path(account.pw_dir):
        raise SetupError("_REMOTE_USER_HOME must match the configured user account home.")
    config = {
        "options": options,
        "user": user,
        "user_home": str(home),
        "install_user": pwd.getpwuid(os.getuid()).pw_name,
        "python_path": str(Path(sys.executable).resolve()),
        "python_version": run(
            [sys.executable, "-c", "import platform; print(platform.python_version())"], capture=True
        ),
    }
    # Operational values must round-trip exactly (notably SSH usernames).
    # Options reject embedded credentials; displayed diagnostics are sanitized.
    save_json(CONFIG, config)
    if options["mode"] == "source":
        owned = home / ".local/share/avd"
        # Own only Feature-created directories, never existing home/workspace parents.
        missing = [path for path in (home / ".local", home / ".local/share", owned) if not path.exists()]
        owned.mkdir(parents=True, exist_ok=True)
        for path in missing:
            os.chown(path, account.pw_uid, account.pw_gid)
            path.chmod(0o755)
        target = owned / "venv"
        if VENV.is_symlink():
            if VENV.readlink() != target:
                raise SetupError("Existing source environment points to a different home. Rebuild the container.")
        elif VENV.exists():
            raise SetupError("A different environment already occupies /opt/avd/venv. Rebuild the container.")
        else:
            VENV.symlink_to(target)
        display({**context(config), "phase": "source preparation", "status": "deferred"})
    else:
        state_path(config).unlink(missing_ok=True)
        complete(config, environment(config))


def setup_source(config):
    if config["options"]["mode"] != "source":
        return
    account = pwd.getpwnam(config["user"])
    if os.getuid() != account.pw_uid:
        raise SetupError(f"Run avd-setup-source as configured user {config['user']}; sudo is not required.")
    state_path(config).unlink(missing_ok=True)
    source = checkout(config)
    source_metadata(source)
    report = environment(config, source)
    complete(config, report, source)


def info(config, as_json):
    report = context(config)
    state = state_path(config)
    if not (VENV / "bin/python").exists():
        if config["options"]["mode"] == "source" and not state.exists():
            report["status"] = "deferred"
        else:
            report["status"] = "invalid"
            report["errors"] = ["The Feature interpreter is missing; rerun setup or rebuild the container."]
    else:
        try:
            report.update(venv_probe())
            if report["python_version"] != config["python_version"]:
                report["errors"].append("Active Python differs from the managed interpreter selection")
            report["status"] = "verified" if state.exists() and not report["errors"] else "incomplete"
            if state.exists():
                previous = read_json(state)
                report["last_installation"] = {
                    key: previous[key] for key in ("completed_at", "source") if key in previous
                }
                if "source" in previous:
                    report["source_activation"] = "complete"
                    source = Path(previous["source"]["root"])
                    report["source"] = {
                        "root": str(source),
                        **git_details(source),
                        "fingerprints": fingerprints(source),
                    }
                    if report["source"] != previous["source"]:
                        report["status"] = "source changed: rerun avd-setup-source after dependency changes"
                else:
                    config = {**config, "resolved_avd": previous["resolved_avd"]}
                    report["resolved_avd"] = config["resolved_avd"]
                try:
                    validate_environment(
                        config, report, Path(previous["source"]["root"]) if "source" in previous else None
                    )
                except SetupError as exc:
                    if not report["errors"]:
                        report["errors"].append(str(exc))
                if report["errors"]:
                    report["status"] = "invalid"
        except SetupError as exc:
            report["status"] = "invalid"
            report["errors"] = [str(exc)]
    display(report, as_json)
    return 1 if report.get("errors") else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("install", "avd-setup-source", "avd-info", "probe", "resolve", "preflight", "invalidate")
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--paths", action="store_true")
    parser.add_argument("--specifier", nargs=2)
    parser.add_argument("--source-metadata")
    args = parser.parse_args()
    os.umask(0o022)
    try:
        if args.command in ("preflight", "invalidate"):
            config = read_json(CONFIG)
            if args.command == "preflight":
                if config["options"] != options_from_env(os.environ):
                    raise SetupError(
                        "Contradictory duplicate Feature installation. Use one option set and rebuild the container."
                    )
            else:
                state_path(config).unlink(missing_ok=True)
        elif args.command == "probe":
            print(json.dumps(probe(args)))
        elif args.command == "resolve":
            print(resolve_release("latest"))
        elif args.command == "install":
            install()
        else:
            config = read_json(CONFIG)
            if args.command == "avd-setup-source":
                setup_source(config)
            else:
                return info(config, args.json)
    except (SetupError, OSError, ValueError, KeyError) as exc:
        emit(f"AVD {args.command} failed: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
