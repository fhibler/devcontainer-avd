"""Checkout convergence and work preservation with real Git repositories."""

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_setup import avd


class GitSource(unittest.TestCase):
    def test_reuse_dirty_checkout_and_ref_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = root / "upstream"
            upstream.mkdir()

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", str(upstream), *args], text=True, stderr=subprocess.DEVNULL
                ).strip()

            git("init", "-b", "main")
            file = upstream / "tracked"
            file.write_text("first\n")
            git("add", "tracked")
            git("-c", "user.name=Florian Hibler", "-c", "user.email=florian@arista.com", "commit", "-m", "first")
            first = git("rev-parse", "HEAD")
            git("tag", "first")
            file.write_text("second\n")
            git("add", "tracked")
            git("-c", "user.name=Florian Hibler", "-c", "user.email=florian@arista.com", "commit", "-m", "second")
            config = {
                "user_home": str(root / "home"),
                "options": {
                    "sourceType": "git",
                    "sourceRepository": "https://example.test/avd.git",
                    "sourceRef": "first",
                },
            }
            env = {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"url.{upstream}.insteadOf",
                "GIT_CONFIG_VALUE_0": "https://example.test/avd.git",
            }
            with patch.dict(os.environ, env), contextlib.redirect_stdout(io.StringIO()):
                config["options"]["sourceRef"] = "missing"
                with self.assertRaises(avd.SetupError):
                    avd.checkout(config)
                self.assertEqual(list((root / "home/.local/share/avd").iterdir()), [])
                config["options"]["sourceRef"] = "first"
                source = avd.checkout(config)
                self.assertEqual(avd.git_details(source)["commit"], first)
                (source / "tracked").write_text("user work\n")
                self.assertEqual(avd.checkout(config), source)
                config["options"]["sourceRef"] = "main"
                with self.assertRaisesRegex(avd.SetupError, "No reset/clean"):
                    avd.checkout(config)
                self.assertEqual((source / "tracked").read_text(), "user work\n")
                self.assertEqual(avd.git_details(source)["commit"], first)
                (source / "tracked").write_text("first\n")
                (source / "untracked").write_text("keep me\n")
                with self.assertRaisesRegex(avd.SetupError, "No reset/clean"):
                    avd.checkout(config)
                self.assertEqual((source / "untracked").read_text(), "keep me\n")
                (source / "untracked").unlink()
                avd.checkout(config)
                self.assertEqual((source / "tracked").read_text(), "second\n")
                config["options"]["sourceRepository"] = "https://example.test/different.git"
                with self.assertRaisesRegex(avd.SetupError, "different origin"):
                    avd.checkout(config)


if __name__ == "__main__":
    unittest.main()
