"""Cache boundaries: identities, corruption, expiry and portable archives."""

import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_setup import avd

SPEC = importlib.util.spec_from_file_location("avd_cache", Path(__file__).resolve().parents[2] / "src/avd/avd_cache.py")
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)


class Downloads(unittest.TestCase):
    def test_metadata_hit_avoids_the_network_and_missing_entry_falls_back(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"AVD_DOWNLOAD_CACHE": directory, "AVD_CACHE_RECORD": directory}),
            patch.object(avd.urllib.request, "urlopen") as request,
        ):
            avd.cache.record_metadata("https://example.test/hit", {"value": 1})
            self.assertEqual(avd.http_json("https://example.test/hit"), {"value": 1})
            request.assert_not_called()
            request.return_value.__enter__.return_value = io.StringIO('{"value": 2}')
            self.assertEqual(avd.http_json("https://example.test/miss"), {"value": 2})
            self.assertEqual(avd.cache.metadata("https://example.test/miss"), {"value": 2})

    def test_local_galaxy_install_uses_all_tarballs_without_dependency_api_calls(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"AVD_DOWNLOAD_CACHE": directory}),
            patch.object(avd, "venv_probe", return_value={"site_packages": directory, "ansible": "2.20.0"}),
            patch.object(avd, "run") as run,
            patch.object(
                avd.cache, "collections", return_value=[Path(directory) / "a.tar.gz", Path(directory) / "b.tar.gz"]
            ),
        ):
            avd.install_collections({"arista.avd": "6.4.0"})
            command = run.call_args.args[0]
            self.assertIn("--no-deps", command)
            self.assertIn(Path(directory) / "b.tar.gz", command)
            self.assertNotIn("-r", command)
            run.assert_called_once()

    def test_uv_seed_is_writable_separate_and_only_copied_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed, destination = root / "seed", root / "user-cache"
            (seed / "uv").mkdir(parents=True)
            (seed / "uv/wheel").write_bytes(b"wheel")
            (seed / "uv/link").symlink_to("wheel")
            destination.mkdir()
            (destination / "link").symlink_to("wheel")
            cache.write_checksums(seed)
            with patch.dict(os.environ, {"AVD_DOWNLOAD_CACHE": str(seed)}):
                cache.seed_uv(destination)
                (destination / "wheel").write_bytes(b"user-writable")
                cache.seed_uv(destination)
            self.assertEqual((seed / "uv/wheel").read_bytes(), b"wheel")
            self.assertEqual((destination / "wheel").read_bytes(), b"user-writable")

    def test_metadata_is_scoped_to_url_and_expires_daily(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"AVD_DOWNLOAD_CACHE": directory, "AVD_CACHE_RECORD": directory}),
        ):
            url = "https://example.test/metadata"
            self.assertIsNone(cache.metadata(url))
            cache.record_metadata(url, {"versions": ["1.0.0"]})
            self.assertEqual(cache.metadata(url), {"versions": ["1.0.0"]})
            self.assertIsNone(cache.metadata(url + "/other"))
            with patch.object(cache, "today", return_value="2099-01-01"):
                self.assertIsNone(cache.metadata(url))

    def test_collection_identity_and_corrupt_archives(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AVD_DOWNLOAD_CACHE": directory}):
            requirements = {"arista.avd": "6.4.0"}
            location = Path(directory) / "galaxy" / cache.collection_key(requirements, "2.20.0")
            location.mkdir(parents=True)
            archive = location / "arista-avd-6.4.0.tar.gz"
            archive.write_bytes(b"collection archive")
            cache.write_json(
                location / "manifest.json",
                {
                    "requirements": requirements,
                    "ansible": "2.20.0",
                    "epoch": cache.today(),
                    "archives": [{"name": archive.name, "sha256": cache.sha256(archive)}],
                },
            )
            self.assertEqual(cache.collections(requirements, "2.20.0"), [archive])
            self.assertIsNone(cache.collections(requirements, "2.19.0"))
            self.assertIsNone(cache.collections({"arista.avd": "6.5.0"}, "2.20.0"))
            archive.write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "checksum"):
                cache.collections(requirements, "2.20.0")

    def test_bundle_round_trip_preserves_executable_bits_and_internal_links(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "source"
            source.mkdir()
            (source / "python").write_bytes(b"interpreter")
            (source / "other-python").write_bytes(b"interpreter")
            (source / "python").chmod(0o755)
            (source / "python3").symlink_to("python")
            archive = directory / "downloads.tgz"
            cache.pack(source, archive)
            restored = directory / "restored"
            cache.unpack(archive, restored)
            self.assertEqual((restored / "python").stat().st_mode & 0o777, 0o755)
            self.assertEqual((restored / "python3").readlink(), Path("python"))
            (restored / "python3").unlink()
            (restored / "python3").symlink_to("other-python")
            with self.assertRaisesRegex(ValueError, "link integrity"):
                cache.verify(restored)
            (restored / "python3").unlink()
            (restored / "python3").symlink_to("python")
            (restored / "extra").write_bytes(b"unindexed")
            with self.assertRaisesRegex(ValueError, "integrity"):
                cache.verify(restored)
            archive.write_bytes(b"corrupt")
            with self.assertRaisesRegex(ValueError, "checksum"):
                cache.unpack(archive, directory / "corrupt")

    def test_escaping_links_and_unsafe_tar_members_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "source"
            source.mkdir()
            (directory / "outside").write_bytes(b"outside")
            (source / "escape").symlink_to("../outside")
            with self.assertRaisesRegex(ValueError, "escapes"):
                cache.write_checksums(source)
            archive = directory / "unsafe.tgz"
            with tarfile.open(archive, "w:gz") as stream:
                member = tarfile.TarInfo("../escape")
                member.size = 1
                stream.addfile(member, io.BytesIO(b"x"))
            with self.assertRaises(tarfile.FilterError):
                cache.extract(archive, directory / "restored")

    def test_source_cache_requires_exact_commit_repository_and_checksum(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AVD_DOWNLOAD_CACHE": directory}):
            commit = "a" * 40
            root = Path(directory) / "sources"
            root.mkdir()
            archive = root / (commit + ".tgz")
            archive.write_bytes(b"source")
            (root / (commit + ".json")).write_text(
                json.dumps(
                    {"commit": commit, "repository": "https://example.test/avd", "sha256": cache.sha256(archive)}
                )
            )
            self.assertEqual(cache.source("https://example.test/avd", commit), archive)
            self.assertIsNone(cache.source("https://other.test/avd", commit))
            self.assertIsNone(cache.source("https://example.test/avd", "main"))
            archive.write_bytes(b"bad")
            with self.assertRaisesRegex(ValueError, "checksum"):
                cache.source("https://example.test/avd", commit)


if __name__ == "__main__":
    unittest.main()
