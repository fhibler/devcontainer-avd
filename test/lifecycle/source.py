"""Real contributor workflows in disposable AVD checkouts."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*args, cwd):
    subprocess.run(args, cwd=cwd, check=True)


def main():
    from zoneinfo import ZoneInfo

    assert str(ZoneInfo("UTC")) == "UTC"
    root = Path(sys.argv[1])
    report = json.loads(subprocess.check_output(["avd-info", "--json"], text=True))
    assert report["running_from_source"]
    assert Path(report["pyavd_path"]).is_relative_to(root / "python-avd")
    assert Path(report["collection_path"]) == root / "ansible_collections/arista/avd"
    assert list((root / "python-avd/pyavd").rglob("*.pickle")), "Editable build did not compile schemas"
    from pyavd.constants import EOS_CLI_CONFIG_GEN_JINJA2_PRECOMPILED_TEMPLATE_PATH

    compiled = Path(EOS_CLI_CONFIG_GEN_JINJA2_PRECOMPILED_TEMPLATE_PATH)
    assert compiled.exists(), "Editable build did not compile templates"
    run(sys.executable, "-m", "pytest", "-q", "python-avd/tests/pyavd/utils/test_get.py", cwd=root)
    with tempfile.TemporaryDirectory() as directory:
        run("mkdocs", "build", "--site-dir", directory, cwd=root)
    # Isolated Python hook, Node hook, and old-Ansible documentation hook.
    for hook, file in [
        ("trailing-whitespace", "README.md"),
        ("markdownlint-cli2", "README.md"),
        ("docs-plugin-modules", "ansible_collections/arista/avd/plugins/modules/verify_requirements.py"),
    ]:
        run("pre-commit", "run", hook, "--files", file, cwd=root)
    template = root / "python-avd/pyavd/_eos_cli_config_gen/j2templates/eos-intended-config.j2"
    original = template.read_text()
    try:
        template.write_text(original + "\n! avd-feature-live-template-edit\n")
        run("make", "-C", "python-avd", "dep", cwd=root)
        config = subprocess.check_output(
            [sys.executable, "-c", 'import pyavd; print(pyavd.get_device_config({"hostname":"live-test"}))'],
            text=True,
            cwd=root,
        )
        assert "! avd-feature-live-template-edit" in config
    finally:
        template.write_text(original)
        run("make", "-C", "python-avd", "dep", cwd=root)
    # Preserve a real user change across subsequent source setup invocations.
    readme = root / "README.md"
    readme.write_text(readme.read_text() + "\nAVD Feature lifecycle preservation probe.\n")
    # Dependency-input change must be recorded and reconciled on explicit rerun.
    pyproject = root / "pyproject.toml"
    pyproject.write_text(pyproject.read_text() + "\n# Lifecycle dependency fingerprint probe\n")
    print("Source contributor assertions passed")


if __name__ == "__main__":
    main()
