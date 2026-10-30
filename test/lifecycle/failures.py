"""Source entry-point failure must invalidate success without discarding work."""

import importlib.metadata as md
import json
import subprocess
from pathlib import Path


def main():
    report = json.loads(subprocess.check_output(["avd-info", "--json"], text=True))
    source = Path(report["source"]["root"])
    state = Path(report["development_home"]) / ".local/share/avd/state.json"
    original_readme = (source / "README.md").read_text()
    pyproject = source / "pyproject.toml"
    original = pyproject.read_text()
    try:
        pyproject.rename(source / "pyproject.toml.avd-test-backup")
        missing = subprocess.run(["avd-setup-source"], text=True, capture_output=True)
        assert missing.returncode != 0 and "source metadata" in missing.stdout, missing.stdout
        assert not state.exists()
        (source / "pyproject.toml.avd-test-backup").rename(pyproject)
        pyproject.write_text("[invalid TOML")
        malformed = subprocess.run(["avd-setup-source"], text=True, capture_output=True)
        assert malformed.returncode != 0 and "source metadata" in malformed.stdout, malformed.stdout
        assert not state.exists()
        assert (source / "README.md").read_text() == original_readme
    finally:
        backup = source / "pyproject.toml.avd-test-backup"
        if backup.exists():
            backup.rename(pyproject)
        pyproject.write_text(original)
    subprocess.run(["avd-setup-source"], check=True)
    assert state.exists()
    distribution = md.distribution("pyavd")
    direct_url_file = next(
        distribution.locate_file(file) for file in distribution.files if file.name == "direct_url.json"
    )
    original_url = direct_url_file.read_text()
    try:
        altered = json.loads(original_url)
        altered["dir_info"]["editable"] = False
        direct_url_file.write_text(json.dumps(altered))
        wrong_origin = subprocess.run(["avd-info", "--json"], text=True, capture_output=True)
        assert wrong_origin.returncode != 0 and "editable" in wrong_origin.stdout, wrong_origin.stdout
    finally:
        direct_url_file.write_text(original_url)
    subprocess.run(["avd-info", "--json"], check=True, stdout=subprocess.DEVNULL)
    assert state.exists()
    print("Source failure and recovery assertions passed")


if __name__ == "__main__":
    main()
