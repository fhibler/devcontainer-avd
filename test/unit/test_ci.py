"""Exercise actual CI scenario assembly without a container engine."""

import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("ci", Path(__file__).resolve().parents[2] / "scripts/ci.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


class LifecycleConfiguration(unittest.TestCase):
    def test_diagnostic_artifacts_are_sanitized_without_corrupting_json(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TEST_TOKEN": "diagnostic-secret"}):
            path = Path(directory) / "diagnostic.json"
            ci.write(path, {"message": '"https://user:password@example.test/path?token=hidden" diagnostic-secret'})
            self.assertEqual(json.loads(path.read_text()), {"message": '"https://example.test/path" [redacted]'})

    def test_every_scenario_builds_valid_feature_options(self):
        scenarios = json.loads((ci.ROOT / "test/scenarios.json").read_text())
        provenance = {"avd": "6.5.0", "baseline": "a" * 40, "devel": "b" * 40}
        for scenario in scenarios:
            with self.subTest(scenario=scenario["name"]):
                original = copy.deepcopy(scenario)
                config = ci.lifecycle_config(scenario, provenance)
                options = config["features"]["./avd"]
                parsed = ci.setup.options_from_env(
                    {
                        key.upper(): str(value).lower() if isinstance(value, bool) else value
                        for key, value in options.items()
                    }
                )
                self.assertEqual(scenario, original)
                self.assertEqual(config["image"], scenario["image"])
                self.assertEqual(config["waitFor"], "postCreateCommand")
                self.assertTrue(config["postCreateCommand"])
                if parsed["mode"] == "source":
                    if parsed["sourceType"] == "git":
                        self.assertEqual(parsed["sourceRef"], provenance[scenario["source"]])
                else:
                    self.assertEqual(parsed["version"], scenario["options"].get("version", provenance["avd"]))
                if scenario.get("user"):
                    self.assertEqual(config["remoteUser"], scenario["user"])
                if scenario.get("uidAdjustment"):
                    self.assertTrue(config["updateRemoteUserUID"])
                if scenario.get("compose"):
                    self.assertIn("ghcr.io/devcontainers/features/python:1", config["features"])
                    self.assertIn("ghcr.io/devcontainers/features/common-utils:2", config["features"])


if __name__ == "__main__":
    unittest.main()
