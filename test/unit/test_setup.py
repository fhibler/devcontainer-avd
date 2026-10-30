"""Deterministic contract tests against the shipped helper, not a test resolver."""

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("avd_setup", Path(__file__).resolve().parents[2] / "src/avd/avd_setup.py")
avd = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(avd)


class Options(unittest.TestCase):
    def test_defaults_and_standard_mapping(self):
        options = avd.options_from_env({})
        self.assertEqual(options["mode"], "runtime")
        self.assertEqual(avd.python_request(options), "3.14")
        options = avd.options_from_env({"MODE": "source", "INSTALLSSHSUPPORT": "true"})
        self.assertTrue(options["installSshSupport"])
        self.assertEqual(avd.python_request(options), "3.12")
        self.assertEqual(avd.python_request(avd.options_from_env({"PYTHONVERSION": "3.13.9"})), "3.13.9")

    def test_invalid_or_contradictory_options(self):
        cases = [
            {"MODE": "other"},
            {"MODE": "source", "VERSION": "6.4.0"},
            {"SOURCETYPE": "git"},
            {"SOURCEPATH": "/source"},
            {"SOURCEREF": "v6.4.0"},
            {"MODE": "source", "SOURCETYPE": "git", "SOURCEPATH": "/source"},
            {"MODE": "source", "PYTHONVERSION": "3.14"},
            {"PYTHONVERSION": "3.11"},
            {"PYTHONVERSION": "3.12; id"},
            {"VERSION": ">=6.4"},
            {"VERSION": "6.4.0rc1"},
            {"VERSION": "6.3.0"},
            {"VERSION": "7.0.0"},
            {"INSTALLSSHSUPPORT": "yes"},
            {"MODE": "source", "SOURCETYPE": "git", "SOURCEREF": "--help"},
            {"MODE": "source", "SOURCETYPE": "git", "SOURCEREF": "HEAD~1"},
            {"SOURCEPATH": "\nsecret"},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(avd.SetupError):
                avd.options_from_env(case)

    def test_repository_credentials_and_protocols(self):
        for repo in [
            "https://github.com/aristanetworks/avd.git",
            "ssh://git@example.test/avd.git",
            "git@example.test:avd.git",
        ]:
            avd.validate_repository(repo)
        for repo in [
            "http://example.test/avd",
            "/tmp/repo",
            "file:///repo",
            "https://user:secret@example.test/repo",
            "ssh://git:secret@example.test/repo",
            "ssh://git:@example.test/repo",
            "https://example.test/a b",
            "https://example.test/repo?token=secret",
            "https://example.test/repo#secret",
            "https://@example.test/repo",
            "https://[invalid/repo",
            "ssh://git@example.test:invalid/repo",
            "https://example.test:99999/repo",
        ]:
            with self.subTest(repo=repo), self.assertRaises(avd.SetupError):
                avd.validate_repository(repo)


class ReleaseSelection(unittest.TestCase):
    def setUp(self):
        self.pypi = {
            "releases": {version: [{"yanked": False}] for version in ["6.4.0", "6.5.0", "6.10.0", "6.11.0rc1"]}
        }
        self.galaxy = [{"version": version} for version in ["6.4.0", "6.5.0", "6.10.0", "6.12.0"]]

    def test_semantic_intersection_and_exact_selection(self):
        self.assertEqual(avd.select_release("latest", self.pypi, self.galaxy), "6.10.0")
        self.assertEqual(avd.select_release("6.4.0", self.pypi, self.galaxy), "6.4.0")
        with self.assertRaises(avd.SetupError):
            avd.select_release("6.12.0", self.pypi, self.galaxy)

    def test_yanked_and_empty_release_files(self):
        self.pypi["releases"]["6.10.0"] = [{"yanked": True}]
        self.assertEqual(avd.select_release("latest", self.pypi, self.galaxy), "6.5.0")
        self.pypi["releases"]["6.10.0"].append({"yanked": False})
        self.assertEqual(avd.select_release("latest", self.pypi, self.galaxy), "6.10.0")
        self.pypi["releases"]["6.10.0"] = []
        with self.assertRaises(avd.SetupError):
            avd.select_release("6.10.0", self.pypi, self.galaxy)

    def test_new_major_is_not_silently_downgraded(self):
        self.pypi["releases"]["7.0.0"] = [{"yanked": False}]
        self.galaxy.append({"version": "7.0.0"})
        with self.assertRaisesRegex(avd.SetupError, "update the Feature"):
            avd.select_release("latest", self.pypi, self.galaxy)

    def test_malformed_metadata_and_empty_intersection(self):
        for pypi, galaxy in [({}, []), ({"releases": {"6.4.0": [{}]}}, []), (self.pypi, [{}]), ({"releases": {}}, [])]:
            with self.subTest(pypi=pypi), self.assertRaises(avd.SetupError):
                avd.select_release("latest", pypi, galaxy)


class FailureBoundaries(unittest.TestCase):
    def test_transient_http_only_is_retried(self):
        import urllib.error

        for code, count in [(404, 1), (503, 3)]:
            error = urllib.error.HTTPError(avd.PYPI, code, "error", {}, None)
            self.addCleanup(error.close)
            with (
                patch.object(avd.urllib.request, "urlopen", side_effect=error) as request,
                patch.object(avd.time, "sleep"),
            ):
                with self.assertRaises(avd.SetupError):
                    avd.http_json(avd.PYPI)
                self.assertEqual(request.call_count, count)

    def test_resolver_subprocess_failure_does_not_look_successful(self):
        boundary = subprocess.CompletedProcess(["uv"], 1, "dependency conflict")
        with (
            patch.object(subprocess, "run", return_value=boundary),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            with self.assertRaisesRegex(avd.SetupError, "exit 1"):
                avd.run(["uv", "pip", "install", "pyavd"])
        self.assertIn("dependency conflict", output.getvalue())

    def test_source_metadata_requires_real_layout_and_groups(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(avd.SetupError):
                avd.source_metadata(root)
            (root / "python-avd").mkdir()
            (root / "python-avd/pyproject.toml").write_text('[project]\nname="pyavd"\n')
            (root / "pyproject.toml").write_text("[dependency-groups]\ndev=[]\ndoc=[]\ncoverage=[]\n")
            with self.assertRaisesRegex(avd.SetupError, "real ansible_collections"):
                avd.source_metadata(root)
            (root / "ansible_collections/arista/avd/plugins").mkdir(parents=True)
            (root / "ansible_collections/arista/avd/plugins/__init__.py").touch()
            (root / "ansible_collections/arista/avd/galaxy.yml").touch()
            self.assertEqual(avd.source_metadata(root)["project"]["name"], "pyavd")

    def test_malformed_collection_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "ansible_collections/arista/avd/galaxy.yml"
            path.parent.mkdir(parents=True)
            for text in ("[invalid", "{}", "namespace: arista\nname: avd\nversion: 6.4.0\ndependencies: []\n"):
                path.write_text(text)
                with self.subTest(text=text), self.assertRaises(avd.SetupError):
                    avd.source_collection_metadata(root)
            path.write_text("namespace: arista\nname: avd\nversion: 6.4.0\ndependencies:\n  arista.eos: '>=12.0.0'\n")
            self.assertEqual(avd.source_collection_metadata(root)["dependencies"], {"arista.eos": ">=12.0.0"})

    def test_json_redaction_preserves_escaped_quotes(self):
        report = {"history": 'curl "https://user:password@example.test/path?token=hidden"; echo "quoted"'}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            avd.display(report, as_json=True)
        self.assertEqual(json.loads(output.getvalue()), {"history": 'curl "https://example.test/path"; echo "quoted"'})

    def test_secrets_are_removed_from_subprocess_output(self):
        with patch.dict(os.environ, {"FAKE_TOKEN": "recognizable-secret"}):
            text = "https://user:password@example.test/path?token=hidden authorization: Bearer fakeauth recognizable-secret"
            with (
                patch.object(subprocess, "run", return_value=subprocess.CompletedProcess(["tool"], 1, text)),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                with self.assertRaises(avd.SetupError):
                    avd.run(["tool"])
            result = output.getvalue()
            for secret in ("password", "hidden", "fakeauth", "recognizable-secret"):
                self.assertNotIn(secret, result)
            self.assertIn("https://example.test/path", result)


if __name__ == "__main__":
    unittest.main()
