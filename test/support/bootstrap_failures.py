"""Controlled bootstrap and interpreter failures in disposable containers."""

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def main():
    with tempfile.TemporaryDirectory(prefix="avd-failures-") as directory:
        directory = Path(directory)
        payload = directory / "feature"
        shutil.copytree("/tmp/feature", payload)
        bin_dir = directory / "bin"
        bin_dir.mkdir()
        env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
        options = json.loads(Path("/opt/avd/config.json").read_text())["options"]
        env.update(
            {name.upper(): str(value).lower() if isinstance(value, bool) else value for name, value in options.items()}
        )

        def wrapper(name, text):
            path = bin_dir / name
            path.write_text("#!/bin/sh\n" + text + "\n")
            path.chmod(0o755)

        def fails(message):
            output = subprocess.run(["bash", str(payload / "install.sh")], env=env, text=True, capture_output=True)
            assert output.returncode != 0, output.stdout
            assert message in output.stdout + output.stderr, output.stdout + output.stderr
            assert "Feature 1.0.0: ready" not in output.stdout

        wrapper("uname", 'if [ "$1" = -s ]; then echo Linux; else echo ppc64le; fi')
        fails("only amd64 and arm64")
        (bin_dir / "uname").unlink()
        release = Path("/etc/os-release")
        original = release.read_text()
        try:
            release.write_text("ID=unsupported\n")
            fails("Debian/Ubuntu or RHEL-family")
        finally:
            release.write_text(original)
        # Package-manager boundaries are bypassed only to reach deterministic
        # bootstrap failures; happy-path platform installs always use real ones.
        for manager in ("apt-get", "dnf", "microdnf"):
            wrapper(manager, "exit 0")
        wrapper("curl", "exit 22")
        fails("failed during bootstrap")
        assert not Path("/opt/avd/state.json").exists()
        wrapper("curl", 'while [ "$1" != -o ]; do shift; done\nshift\nprintf corrupt > "$1"')
        fails("checksum mismatch")
        assert not Path("/opt/avd/state.json").exists()
        for values in [
            {"MODE": "source", "VERSION": "6.4.0"},
            {"INSTALLSSHSUPPORT": "yes"},
            {"PYTHONVERSION": "3.99"},
            {"MODE": "source", "SOURCETYPE": "git", "SOURCEREF": "HEAD~1"},
        ]:
            previous = env.copy()
            env.update(values)
            fails("AVD:")
            env = previous
        interpreter = Path("/opt/avd/venv/bin/python")
        backup = interpreter.with_name("python.avd-test-backup")
        interpreter.rename(backup)
        try:
            missing = subprocess.run(["avd-info", "--json"], text=True, capture_output=True)
            assert missing.returncode != 0, missing.stdout
            assert "interpreter is missing" in json.loads(missing.stdout)["errors"][0], missing.stdout
        finally:
            backup.rename(interpreter)
        config = Path("/opt/avd/config.json")
        original_config = config.read_text()
        try:
            altered = json.loads(original_config)
            altered["python_version"] = "3.12.0"
            config.write_text(json.dumps(altered))
            mismatch = subprocess.run(["avd-info", "--json"], text=True, capture_output=True)
            assert mismatch.returncode != 0, mismatch.stdout
            assert "Active Python differs" in json.loads(mismatch.stdout)["errors"][0], mismatch.stdout
        finally:
            config.write_text(original_config)
    print("Bootstrap failure assertions passed")


if __name__ == "__main__":
    main()
