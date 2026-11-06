"""Recreate a runtime from download inputs in a network-disabled snapshot."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    prefix = Path("/opt/avd")
    options = json.loads((prefix / "config.json").read_text())["options"]
    assert options["mode"] == "runtime"
    assert (prefix / "download-cache/SHA256SUMS").is_file()
    env = dict(os.environ, UV_OFFLINE="true", AVD_CACHE_RECORD="")
    env.update(
        {key.upper(): str(value).lower() if isinstance(value, bool) else value for key, value in options.items()}
    )
    with tempfile.TemporaryDirectory(prefix="avd-offline-bootstrap-") as directory:
        # The snapshot already has real OS prerequisites. Only OS transactions
        # are skipped here; bootstrap, Python, PyPI and Galaxy inputs must be reused.
        for name in ("apt-get", "dnf", "microdnf", "curl", "git"):
            wrapper = Path(directory) / name
            wrapper.write_text("#!/bin/sh\nexit " + ("97" if name in ("curl", "git") else "0") + "\n")
            wrapper.chmod(0o755)
        env["PATH"] = directory + ":" + env["PATH"]
        for name in ("config.json", "state.json", "bootstrap-python"):
            (prefix / name).unlink(missing_ok=True)
        for name in ("venv", "python", "bootstrap", "cache"):
            shutil.rmtree(prefix / name, ignore_errors=True)
        # The parent interpreter is already running; the installer must restore
        # fresh executables from the verified bundle before launching subprocesses.
        subprocess.run(["bash", "/tmp/feature/install.sh"], env=env, check=True)
        result = subprocess.check_output(["/usr/local/bin/avd-info", "--json"], env=env, text=True)
        assert json.loads(result)["status"] == "verified", result
    print("Network-free bootstrap, Python, wheel and Galaxy reuse passed")


if __name__ == "__main__":
    main()
