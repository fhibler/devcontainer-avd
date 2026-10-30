"""Operational identity must survive persistence and current-environment checks."""

import contextlib
import copy
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_setup import avd


class Inspection(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.prefix = self.root / "opt/avd"
        self.venv = self.prefix / "venv"
        (self.venv / "bin").mkdir(parents=True)
        (self.venv / "bin/python").touch()
        for name, value in {"PREFIX": self.prefix, "VENV": self.venv, "CONFIG": self.prefix / "config.json"}.items():
            self.enterContext(patch.object(avd, name, value))
        self.enterContext(patch("platform.freedesktop_os_release", return_value={"ID": "test"}))
        self.config = {
            "options": avd.options_from_env({}),
            "user": "review-user",
            "user_home": str(self.root / "home"),
            "install_user": "root",
            "python_path": "/managed/python",
            "python_version": "3.14.10",
        }
        site = self.venv / "site-packages"
        self.report = {
            "errors": [],
            "python_version": "3.14.10",
            "site_packages": str(site),
            "packages": {"pyavd": "6.4.0", **dict.fromkeys(avd.TOOLS, "1"), "paramiko": "1", "ansible-pylibssh": "1"},
            "collections": {"arista.avd": "6.4.0", "community.general": "12.0.0"},
            "collection_path": str(site / "ansible_collections/arista/avd"),
            "collection_version": "6.4.0",
            "pyavd_path": str(site / "pyavd/__init__.py"),
            "pyavd_editable": False,
            "pyavd_editable_root": None,
            "running_from_source": False,
        }

    def complete(self, source=None):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            avd.complete({**self.config, "resolved_avd": "6.4.0"}, self.report, source)
        return output.getvalue()

    def inspect(self, report=None):
        with (
            patch.object(avd, "venv_probe", return_value=copy.deepcopy(self.report if report is None else report)),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            result = avd.info(self.config, True)
        return result, json.loads(output.getvalue())

    def test_inspection_checks_required_packages_collections_and_selected_release(self):
        self.config["options"].update(mode="tools", installSshSupport=True, installCommunityGeneral=True)
        self.complete()
        code, report = self.inspect()
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "verified")
        self.assertEqual(report["resolved_avd"], "6.4.0")
        for missing, group in [
            ("molecule", "packages"),
            ("paramiko", "packages"),
            ("community.general", "collections"),
        ]:
            with self.subTest(missing=missing):
                changed = copy.deepcopy(self.report)
                changed[group].pop(missing)
                code, report = self.inspect(changed)
                self.assertEqual(code, 1)
                self.assertEqual(report["status"], "invalid")
                self.assertIn(missing, report["errors"][0])
        changed = copy.deepcopy(self.report)
        changed["packages"]["pyavd"] = changed["collection_version"] = "6.5.0"
        code, report = self.inspect(changed)
        self.assertEqual(code, 1)
        self.assertIn("selected release", report["errors"][0])

    def test_source_identity_and_fingerprints_remain_exact_but_display_is_redacted(self):
        source = self.root / "network/avd"
        source.mkdir(parents=True)
        (source / "pyproject.toml").write_text("# source\n")
        for args in [
            ["init", "-b", "main"],
            ["add", "pyproject.toml"],
            ["-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "first"],
        ]:
            subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)
        self.config["options"] = avd.options_from_env({"MODE": "source", "SOURCEPATH": str(source)})
        self.report.update(
            pyavd_path=str(source / "python-avd/pyavd/__init__.py"),
            pyavd_editable=True,
            pyavd_editable_root=str(source / "python-avd"),
            collection_path=str(source / "ansible_collections/arista/avd"),
            running_from_source=True,
        )
        with patch.dict(os.environ, {"TEST_TOKEN": "network"}):
            output = self.complete(source)
            state = avd.read_json(avd.state_path(self.config))
            self.assertEqual(state["source"]["root"], str(source))
            self.assertNotIn("network", output)
            code, report = self.inspect()
            self.assertEqual((code, report["status"]), (0, "verified"))
            (source / "pyproject.toml").write_text("# dependency change\n")
            code, report = self.inspect()
            self.assertEqual(code, 0)
            self.assertIn("source changed", report["status"])
        changed = copy.deepcopy(self.report)
        changed["pyavd_editable"] = False
        code, report = self.inspect(changed)
        self.assertEqual((code, report["status"]), (1, "invalid"))

    def test_install_preserves_ssh_identity_and_duplicate_preflight(self):
        # Source preparation only needs an OS account; installation defers all
        # network/package work until source activation.
        self.venv.joinpath("bin/python").unlink()
        self.venv.joinpath("bin").rmdir()
        self.venv.rmdir()
        account = SimpleNamespace(
            pw_name="review-user", pw_dir=self.config["user_home"], pw_uid=os.getuid(), pw_gid=os.getgid()
        )
        env = {
            "MODE": "source",
            "SOURCETYPE": "git",
            "SOURCEREPOSITORY": "ssh://git@example.test/avd.git",
            "_REMOTE_USER": "review-user",
            "TEST_TOKEN": "network",
        }
        Path(account.pw_dir).mkdir()
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(avd.pwd, "getpwnam", return_value=account),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            avd.install()
            saved = avd.read_json(avd.CONFIG)
            self.assertEqual(saved["options"], avd.options_from_env(os.environ))
            self.assertEqual(self.venv.readlink(), Path(account.pw_dir) / ".local/share/avd/venv")
            with patch("sys.argv", ["avd_setup.py", "preflight"]):
                self.assertEqual(avd.main(), 0)
            with patch.dict(os.environ, {"SOURCEREF": "other"}), patch("sys.argv", ["avd_setup.py", "preflight"]):
                self.assertEqual(avd.main(), 1)


if __name__ == "__main__":
    unittest.main()
