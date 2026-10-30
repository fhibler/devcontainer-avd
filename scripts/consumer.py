#!/usr/bin/env python3
"""Anonymous consumer test of the immutable released OCI Feature."""

import json
import os
import subprocess
import tempfile
from pathlib import Path

from ci import ARTIFACTS, ENGINE, native_arch, offline_smoke, run


def main():
    native_arch()
    ARTIFACTS.mkdir(exist_ok=True)
    provenance = json.loads((ARTIFACTS / "provenance.json").read_text())
    container = None
    with tempfile.TemporaryDirectory(prefix="avd-consumer-") as directory:
        workspace = Path(directory)
        (workspace / ".devcontainer").mkdir()
        config = {
            "image": "ubuntu:26.10",
            "features": {os.environ["FEATURE_REFERENCE"]: {"version": provenance["avd"]}},
            "waitFor": "postCreateCommand",
        }
        (workspace / ".devcontainer/devcontainer.json").write_text(json.dumps(config))
        try:
            output = run("devcontainer", "up", "--docker-path", ENGINE, "--workspace-folder", workspace)
            container = json.loads(output.splitlines()[-1])["containerId"]
            offline_smoke(container, ARTIFACTS)
        finally:
            if container:
                subprocess.run([ENGINE, "rm", "-f", container], check=False)


if __name__ == "__main__":
    main()
