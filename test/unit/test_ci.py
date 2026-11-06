"""Exercise actual CI scenario assembly without a container engine."""

import copy
import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from collections import Counter
from itertools import product
from pathlib import Path
from unittest.mock import call, patch

import yaml

SPEC = importlib.util.spec_from_file_location("ci", Path(__file__).resolve().parents[2] / "scripts/ci.py")
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


class WorkflowMatrix(unittest.TestCase):
    def prepared_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            outputs = directory / "github-output"
            # Packaging and release resolution are unrelated to matrix coverage.
            (directory / "devcontainer-feature-avd.tgz").write_bytes(b"packaged feature")
            with (
                patch.object(ci, "ARTIFACTS", directory),
                patch.dict(os.environ, {"GITHUB_OUTPUT": str(outputs)}),
                patch.object(ci, "run", return_value="a" * 40),
                patch.object(ci, "extract"),
                patch.object(ci.setup, "resolve_release", return_value="6.4.0"),
            ):
                ci.prepare()
            return dict(line.split("=", 1) for line in outputs.read_text().splitlines())

    def expanded_jobs(self, job, output):
        workflow = yaml.load((ci.ROOT / ".github/workflows/test.yml").read_text(), Loader=yaml.BaseLoader)
        config = workflow["jobs"][job]
        matrix = config["strategy"]["matrix"]
        self.assertEqual(set(matrix), {"runner", "group"}, "Runner and group must be independent matrix axes")
        self.assertEqual(matrix["group"], "${{ fromJSON(needs.prepare.outputs." + output + ") }}")
        self.assertCountEqual(matrix["runner"], ["ubuntu-24.04", "ubuntu-24.04-arm"])
        variable, field = ("TEST_IMAGES", "images") if job == "platform" else ("SCENARIOS", "scenarios")
        selected = [step["env"][variable] for step in config["steps"] if variable in step.get("env", {})]
        self.assertEqual(selected, ["${{ matrix.group." + field + " }}"])
        uploads = [step for step in config["steps"] if step.get("uses", "").startswith("actions/upload-artifact@")]
        self.assertEqual(len(uploads), 1)
        self.assertIn("${{ matrix.group.id }}", uploads[0]["with"]["name"])
        groups = json.loads(self.prepared_outputs()[output])
        self.assertEqual(len({group["id"] for group in groups}), len(groups), "Artifact names must be unique")
        return list(product(matrix["runner"], groups))

    def test_every_platform_image_runs_once_on_each_architecture(self):
        images = json.loads((ci.ROOT / "test/images.json").read_text())
        jobs = self.expanded_jobs("platform", "platform_groups")
        actual = Counter((runner, image) for runner, group in jobs for image in json.loads(group["images"]))
        expected = Counter(product(["ubuntu-24.04", "ubuntu-24.04-arm"], images))
        self.assertEqual(actual, expected)

    def test_every_integration_scenario_runs_once_on_each_architecture(self):
        scenarios = json.loads((ci.ROOT / "test/scenarios.json").read_text())
        by_name = {scenario["name"]: scenario for scenario in scenarios}
        jobs = self.expanded_jobs("integration", "scenario_groups")
        actual = Counter()
        for runner, group in jobs:
            for name in json.loads(group["scenarios"]):
                self.assertEqual(group["image"], by_name[name]["image"])
                actual[runner, name] += 1
        expected = Counter(product(["ubuntu-24.04", "ubuntu-24.04-arm"], by_name))
        self.assertEqual(actual, expected)


class ImageDownloads(unittest.TestCase):
    def test_git_fetch_retries_transport_failure_but_not_missing_ref(self):
        for message, attempts in (("connection reset by peer", 3), ("couldn't find remote ref missing", 1)):
            with (
                self.subTest(message=message),
                patch.object(ci, "run", side_effect=subprocess.CalledProcessError(1, "git", output=message)) as run,
                patch.object(ci.setup.time, "sleep"),
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    ci.download("git", "fetch", "origin", "main")
                self.assertEqual(run.call_count, attempts)

    def test_transient_pull_errors_recover(self):
        for message in (
            "dial tcp: lookup registry-1.docker.io: no such host",
            "net/http: TLS handshake timeout",
            "read: connection reset by peer",
            "received unexpected HTTP status: 503 Service Unavailable",
            "toomanyrequests: Too Many Requests",
            "unexpected EOF",
        ):
            with (
                self.subTest(message=message),
                tempfile.TemporaryDirectory() as directory,
                patch.object(
                    ci, "run", side_effect=[subprocess.CalledProcessError(1, "pull", output=message), "ok"]
                ) as run,
                patch.object(ci.time, "sleep") as sleep,
            ):
                ci.pull_image("almalinux:9.8", Path(directory))
                self.assertEqual(run.call_count, 2)
                sleep.assert_called_once_with(2)
                for invocation in run.call_args_list:
                    self.assertEqual(invocation.args, (ci.ENGINE, "pull", "almalinux:9.8"))
                    self.assertEqual(invocation.kwargs["timeout"], ci.IMAGE_PULL_TIMEOUT_SECONDS)

    def test_persistent_network_errors_and_timeouts_remain_failures(self):
        for error in (
            subprocess.CalledProcessError(1, "pull", output="connection reset by peer"),
            subprocess.TimeoutExpired("pull", ci.IMAGE_PULL_TIMEOUT_SECONDS),
        ):
            with (
                self.subTest(error=error),
                tempfile.TemporaryDirectory() as directory,
                patch.object(ci, "run", side_effect=error) as run,
                patch.object(ci.time, "sleep") as sleep,
            ):
                with self.assertRaises(type(error)):
                    ci.pull_image("almalinux:9.8", Path(directory))
                self.assertEqual(run.call_count, 3)
                self.assertEqual(sleep.call_args_list, [call(2), call(4)])

    def test_permanent_pull_errors_fail_immediately(self):
        for message in ("manifest unknown", "unauthorized: authentication required", "invalid reference format"):
            with (
                self.subTest(message=message),
                tempfile.TemporaryDirectory() as directory,
                patch.object(ci, "run", side_effect=subprocess.CalledProcessError(1, "pull", output=message)) as run,
                patch.object(ci.time, "sleep") as sleep,
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    ci.pull_image("almalinux:9.8", Path(directory))
                run.assert_called_once()
                sleep.assert_not_called()

    def test_failed_pull_is_not_cached(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(ci.IMAGE_IDENTITIES, {}, clear=True),
            patch.object(ci, "pull_image", side_effect=subprocess.CalledProcessError(1, "pull")),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                ci.identity("almalinux:9.8", Path(directory))
            self.assertNotIn("almalinux:9.8", ci.IMAGE_IDENTITIES)


class BuildCaches(unittest.TestCase):
    def test_restore_replace_and_failed_export_preserve_the_last_good_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AVD_BUILD_CACHE": directory}):
            with ci.build_cache("image@sha256:one") as args:
                self.assertNotIn("--cache-from", args)
                destination = Path(args[-1].split("dest=", 1)[1].split(",", 1)[0])
                destination.mkdir()
                (destination / "index.json").write_text("first")
                (destination / "obsolete-blob").touch()
            with ci.build_cache("image@sha256:one") as args:
                self.assertIn("--cache-from", args)
                restored = Path(args[1].removeprefix("type=local,src="))
                self.assertEqual((restored / "index.json").read_text(), "first")
                destination.mkdir()
                (destination / "index.json").write_text("second")
            self.assertFalse((restored / "obsolete-blob").exists())
            with self.assertRaisesRegex(RuntimeError, "build failed"), ci.build_cache("image@sha256:one"):
                destination.mkdir()
                (destination / "index.json").write_text("partial")
                raise RuntimeError("build failed")
            self.assertEqual((restored / "index.json").read_text(), "second")
            self.assertFalse(destination.exists())
            with ci.build_cache("image@sha256:two") as args:
                self.assertNotIn("--cache-from", args)
                destination = Path(args[-1].split("dest=", 1)[1].split(",", 1)[0])
                destination.mkdir()
                (destination / "index.json").write_text("different base")

    def test_cache_misses_do_not_require_a_preexisting_directory(self):
        with patch.dict(os.environ, {"AVD_BUILD_CACHE": ""}), ci.build_cache("image") as args:
            self.assertEqual(args, [])

    def test_dependency_changes_and_daily_refresh_invalidate_install_layers(self):
        provenance = {"avd": "6.4.0", "baseline": "a", "devel": "b", "tools": {"UV": "1"}, "cache_epoch": "2026-11-06"}
        original = ci.build_cache_key(provenance)
        for key, value in (("avd", "6.5.0"), ("devel", "c"), ("tools", {"UV": "2"}), ("cache_epoch", "2026-11-07")):
            with self.subTest(key=key):
                self.assertNotEqual(ci.build_cache_key({**provenance, key: value}), original)
        self.assertEqual(ci.build_cache_key(dict(reversed(list(provenance.items())))), original)

    def test_workflow_cache_is_scoped_to_architecture_and_group_and_saved_only_from_default_branch(self):
        workflow = yaml.load((ci.ROOT / ".github/workflows/test.yml").read_text(), Loader=yaml.BaseLoader)
        for job in ("platform", "integration"):
            steps = workflow["jobs"][job]["steps"]
            restore = next(step for step in steps if step.get("uses", "").startswith("actions/cache/restore@"))
            save = next(step for step in steps if step.get("uses", "").startswith("actions/cache/save@"))
            for prefix in restore["with"]["restore-keys"].splitlines():
                self.assertIn("${{ runner.arch }}", prefix)
                self.assertIn(job + "-${{ matrix.group.id }}", prefix)
                self.assertIn("${{ needs.prepare.outputs.cache_key }}", prefix)
            self.assertIn("success()", save["if"])
            self.assertIn("github.event_name != 'pull_request'", save["if"])
            self.assertIn("github.event.repository.default_branch", save["if"])


class DiagnosticLogs(unittest.TestCase):
    def test_command_logs_survive_success_failure_and_timeout(self):
        for boundary in (
            subprocess.CompletedProcess(["build"], 0, "download completed"),
            subprocess.CompletedProcess(["build"], 1, "download failed"),
            subprocess.TimeoutExpired("build", 10, output=b"partial download output"),
        ):
            with (
                self.subTest(boundary=boundary),
                tempfile.TemporaryDirectory() as directory,
                patch.object(ci, "ARTIFACTS", Path(directory)),
                patch.object(ci, "ACTIVE_TEST_ARTIFACTS", None),
                patch.object(
                    ci.subprocess,
                    "run",
                    **({"side_effect": boundary} if isinstance(boundary, Exception) else {"return_value": boundary}),
                ),
            ):
                log = Path(directory) / "build.log"
                if isinstance(boundary, subprocess.TimeoutExpired):
                    with self.assertRaises(subprocess.TimeoutExpired):
                        ci.run("build", log=log, timeout=10)
                    self.assertIn("partial download output", log.read_text())
                elif boundary.returncode:
                    with self.assertRaises(subprocess.CalledProcessError):
                        ci.run("build", log=log)
                    self.assertIn(boundary.stdout, log.read_text())
                else:
                    self.assertEqual(ci.run("build", log=log), boundary.stdout)
                    self.assertIn(boundary.stdout, log.read_text())

    def test_saved_command_logs_are_redacted(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"TEST_TOKEN": "diagnostic-secret"}),
            patch.object(ci.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "diagnostic-secret")),
        ):
            log = Path(directory) / "build.log"
            ci.run("build", log=log)
            self.assertEqual(log.read_text(), "[redacted]\n")


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
