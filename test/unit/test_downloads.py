"""Download preparation and consumer selection contracts."""

import fnmatch
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("downloads", Path(__file__).resolve().parents[2] / "scripts/downloads.py")
downloads = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(downloads)
ci, cache = downloads.ci, downloads.cache


class Preparation(unittest.TestCase):
    def test_daily_resolution_cleanup_preserves_same_day_downloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, epoch in (("current", cache.today()), ("old", "2000-01-01")):
                cache.write_json(root / "galaxy" / name / "manifest.json", {"epoch": epoch})
            (root / "uv").mkdir()
            (root / "python").mkdir()
            downloads.reset_resolutions(root)
            self.assertTrue((root / "galaxy/current").is_dir())
            self.assertFalse((root / "galaxy/old").exists())
            self.assertTrue((root / "uv").is_dir())
            self.assertTrue((root / "python").is_dir())

    def test_pruning_keeps_downloaded_wheels_and_the_exact_patch_is_not_upgraded(self):
        script = downloads.warmup_script([{"mode": "runtime", "pythonVersion": "3.14.10"}])
        self.assertIn("uv cache prune --cache-dir", script)
        self.assertNotIn("--ci", script)
        self.assertIn("PYTHONVERSION=3.14.10", script)

    def test_groups_select_exactly_the_python_bundles_their_scenarios_need(self):
        scenarios = json.loads((ci.ROOT / "test/scenarios.json").read_text())
        artifacts = ["py3.12", "py3.13", "py3.14"]
        for group in ci.group_scenarios(scenarios):
            selected = [row for row in scenarios if row["name"] in json.loads(group["scenarios"])]
            expected = {"py" + ci.scenario_minor(row["options"]) for row in selected}
            actual = {name for name in artifacts if fnmatch.fnmatchcase(name, group["downloads"])}
            self.assertEqual(actual, expected)
            self.assertEqual(group["sources"], any(row.get("source") for row in selected))

    def test_warmup_preserves_regression_patch_and_source_identities(self):
        provenance = {"avd": "6.5.0", "baseline": "a" * 40, "devel": "b" * 40}
        for python in ("3.12", "3.13", "3.14"):
            for options in downloads.cases(python, provenance):
                self.assertEqual(downloads.minor(options), python)
                parsed = ci.setup.options_from_env(
                    {
                        key.upper(): str(value).lower() if isinstance(value, bool) else value
                        for key, value in options.items()
                    }
                )
                if parsed["mode"] == "source":
                    self.assertIn(parsed["sourceRef"], ("a" * 40, "b" * 40))
        py312 = downloads.cases("3.12", provenance)
        py314 = downloads.cases("3.14", provenance)
        self.assertTrue(any(row.get("version") == "6.4.0" for row in py312))
        self.assertTrue(any(row["pythonVersion"] == "3.14.10" for row in py314))

    def test_source_snapshots_restore_real_git_without_network_and_keep_tags(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            upstream = root / "upstream"
            upstream.mkdir()

            def git(*args):
                return subprocess.check_output(
                    ["git", "-C", upstream, *args], text=True, stderr=subprocess.DEVNULL
                ).strip()

            git("init", "-b", "main")
            (upstream / "tracked").write_text("source\n")
            git("add", "tracked")
            git("-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "fixture")
            git("tag", "v6.4.0")
            commit = git("rev-parse", "HEAD")
            artifacts = root / "artifacts"
            artifacts.mkdir()
            (artifacts / "provenance.json").write_text(json.dumps({"baseline": commit, "devel": commit}))
            repository = "https://example.test/avd.git"
            with (
                patch.dict(
                    os.environ,
                    {
                        "AVD_DOWNLOAD_WORK": str(root / "work"),
                        "AVD_DOWNLOAD_CACHE": str(root / "work/shared"),
                        "GIT_CONFIG_COUNT": "1",
                        "GIT_CONFIG_KEY_0": f"url.{upstream}.insteadOf",
                        "GIT_CONFIG_VALUE_0": repository,
                    },
                ),
                patch.object(downloads, "REPOSITORY", repository),
                patch.object(ci, "ARTIFACTS", artifacts),
            ):
                downloads.sources()
                config = {
                    "user_home": str(root / "home"),
                    "options": {
                        "sourceType": "git",
                        "sourceRepository": repository,
                        "sourceRef": commit,
                    },
                }
                # Remove upstream to prove neither clone nor fetch is needed.
                shutil.rmtree(upstream)
                downloads.sources()
                restored = ci.setup.checkout(config)
                self.assertEqual(ci.setup.git_details(restored), {"commit": commit, "dirty": False})
                self.assertEqual(
                    subprocess.check_output(["git", "-C", restored, "describe", "--tags"], text=True).strip(), "v6.4.0"
                )
                (restored / "tracked").write_text("user work\n")
                self.assertEqual(ci.setup.checkout(config), restored)
                self.assertEqual((restored / "tracked").read_text(), "user work\n")

    def test_multiple_minor_bundles_merge_and_seed_feature_precedes_avd(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AVD_DOWNLOAD_WORK": directory}):
            root = Path(directory)
            for minor in ("312", "314"):
                source = root / minor
                (source / "python").mkdir(parents=True)
                (source / "python" / minor).write_bytes(b"python")
                (source / "metadata").mkdir()
                (source / "metadata/common.json").write_text("{}")
                (source / "uv").mkdir()
                (source / "uv/wheel").write_bytes(b"wheel")
                (source / "uv/link").symlink_to("wheel")
                cache.pack(source, root / "bundles" / f"py{minor}.tgz")
            downloads.merge()
            cache.verify(root / "seed")
            self.assertTrue((root / "seed/python/312").is_file())
            self.assertTrue((root / "seed/python/314").is_file())
            self.assertEqual((root / "seed/uv/link").readlink(), Path("wheel"))
            config = {"image": "ubuntu:24.04", "features": {"./avd": {}}}
            ci.seed_feature(root, config, root / "seed")
            self.assertEqual(config["overrideFeatureInstallOrder"], ["./avd-downloads"])
            self.assertEqual(config["image"], "ubuntu:24.04")
            self.assertTrue((root / "avd-downloads/inputs/python/314").is_file())


if __name__ == "__main__":
    unittest.main()
