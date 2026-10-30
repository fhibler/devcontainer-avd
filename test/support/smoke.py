"""Independent observable assertions, executed using installed controller Python."""

import importlib
import importlib.metadata as md
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


def command(*args, **kwargs):
    try:
        return subprocess.check_output(args, text=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        print(exc.output or "", flush=True)
        raise


def main():
    root = Path(sys.argv[1]).resolve()
    report = json.loads(command("avd-info", "--json"))
    assert report["status"] in ("verified", "source changed: rerun avd-setup-source after dependency changes"), report
    assert not report["errors"], report["errors"]
    assert report["feature_id"] == "avd"
    assert report["raw_architecture"] == platform.machine()
    assert report["host_kernel"] == platform.release()
    assert report["os"]["ID"] == platform.freedesktop_os_release()["ID"]
    assert report["package_manager"] == next(name for name in ("apt-get", "dnf", "microdnf") if shutil.which(name))
    assert report["libc"]["name"] and report["libc"]["version"]
    assert sys.prefix == "/opt/avd/venv" or Path(sys.prefix).resolve() == Path("/opt/avd/venv").resolve()
    assert (
        report["python_version"]
        == command(sys.executable, "-c", "import platform; print(platform.python_version())").strip()
    )
    assert report["packages"]["pyavd"] == md.version("pyavd")
    if report["options"]["mode"] != "source":
        assert md.version("pyavd") == report["collections"]["arista.avd"]
    for name in ("ansible-playbook", "ansible-galaxy", "ansible-doc", "anta"):
        first_line = (Path("/opt/avd/venv/bin") / name).read_text().splitlines()[0]
        assert first_line.startswith("#!"), first_line
        assert Path(first_line[2:]).parent.parent.resolve() == Path("/opt/avd/venv").resolve(), first_line
    for name in ("git", "make", "tar", "curl"):
        command(name, "--version")
    for name in ("anta.cli", "pyavd._anta", "pyavd._cv.client", "pyavd_utils"):
        importlib.import_module(name)
    ssh = report["options"]["installSshSupport"]
    for name in ("paramiko", "ansible-pylibssh"):
        if ssh:
            assert md.version(name)
        elif report["options"]["mode"] == "runtime":
            assert name not in report["packages"]
    if ssh:
        importlib.import_module("pylibsshext.session")
        command("ansible-doc", "-t", "connection", "ansible.netcommon.network_cli")
    assert ("community.general" in report["collections"]) == report["options"]["installCommunityGeneral"]
    if report["options"]["mode"] == "tools":
        for name in ("ansible-lint", "yamllint", "ruff", "pre-commit", "pytest", "tox", "molecule"):
            command(name, "--version")
    for role in ("cv_deploy", "anta_runner", "eos_config_deploy_eapi"):
        assert (Path(report["collection_path"]) / "roles" / role / "tasks/main.yml").is_file()
    assert "eos_designs_facts" in command("ansible-doc", "arista.avd.eos_designs_facts")
    command("anta", "--help")
    for generated in (root / "avd/inventory/intended", root / "avd/inventory/documentation"):
        if generated.exists():
            shutil.rmtree(generated)
    command(
        "ansible-playbook",
        "-i",
        str(root / "avd/inventory/hosts.yml"),
        "-i",
        str(root / "avd/localhost.yml"),
        str(root / "avd/build.yml"),
        cwd=root / "avd",
    )
    config = (root / "avd/inventory/intended/configs/avd-leaf1.cfg").read_text()
    docs = (root / "avd/inventory/documentation/devices/avd-leaf1.md").read_text()
    assert "hostname avd-leaf1" in config and "router bgp 65101" in config, config
    assert "avd-leaf1" in docs and "65101" in docs, docs
    # A consumer's Ansible configuration is honored without Feature overrides.
    project = root / "config-precedence"
    if project.exists():
        shutil.rmtree(project)
    project.mkdir()
    (project / "ansible.cfg").write_text("[defaults]\nforks=7\n")
    precedence = json.loads(command("avd-info", "--json", cwd=project))
    assert precedence["ansible_config"] == str(project / "ansible.cfg"), precedence
    env = dict(os.environ, ANSIBLE_COLLECTIONS_SCAN_SYS_PATH="false")
    disabled = subprocess.run(["avd-info", "--json"], env=env, cwd=project, text=True, capture_output=True)
    assert disabled.returncode != 0, disabled.stdout
    assert "scanning" in disabled.stdout or "ansible_collections" in disabled.stdout, disabled.stdout
    # Deliberately shadow AVD through normal project collection precedence.
    shadow = project / "collections/ansible_collections/arista/avd"
    (shadow / "plugins").mkdir(parents=True, exist_ok=True)
    (shadow / "plugins/__init__.py").write_text("RUNNING_FROM_SOURCE = False\n")
    (shadow / "MANIFEST.json").write_text(json.dumps({"collection_info": {"version": "99.0.0", "dependencies": {}}}))
    (project / "ansible.cfg").write_text("[defaults]\nforks=7\n")
    (project / "shadow.yml").write_text(
        "- hosts: localhost\n  gather_facts: false\n  roles: [arista.avd.eos_designs]\n"
    )
    actual_loader = subprocess.run(
        ["ansible-playbook", "--syntax-check", "-i", "localhost,", "shadow.yml"],
        cwd=project,
        text=True,
        capture_output=True,
    )
    assert actual_loader.returncode != 0, "Real Ansible unexpectedly ignored the local collection override"
    shadowed = subprocess.run(["avd-info", "--json"], cwd=project, text=True, capture_output=True)
    assert shadowed.returncode != 0, shadowed.stdout
    assert "shadow" in shadowed.stdout or "identity" in shadowed.stdout, shadowed.stdout
    for secret in ("avd-test-token-secret", "avd-test-query-secret"):
        assert secret not in command("avd-info", "--json")
    print("AVD smoke assertions passed")


if __name__ == "__main__":
    main()
