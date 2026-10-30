"""Release integrity checks without publishing or registry credentials."""

import contextlib
import copy
import email.message
import hashlib
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.response
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("release", Path(__file__).resolve().parents[2] / "scripts/release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class PayloadIdentity(unittest.TestCase):
    @staticmethod
    def archive(mode=0o755, content=b"payload", name="install.sh", timestamp=0):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as handle:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = mode
            member.mtime = timestamp
            handle.addfile(member, io.BytesIO(content))
        return stream.getvalue()

    def test_normalization_preserves_content_and_modes(self):
        baseline = release.file_map(self.archive())
        self.assertEqual(baseline, release.file_map(self.archive(timestamp=1234)))
        self.assertNotEqual(baseline, release.file_map(self.archive(mode=0o644)))
        self.assertNotEqual(baseline, release.file_map(self.archive(content=b"changed")))
        for name in ["../escape", "/absolute"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.file_map(self.archive(name=name))

    def test_registry_identity(self):
        self.assertEqual(release.registry_path("Owner/Repo"), "owner/repo/avd")
        for value in ["owner/repo/extra", "owner/repo?token=secret", "owner repo/name"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                release.registry_path(value)

    def test_duplicate_normalized_paths_are_rejected(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name in ("install.sh", "./install.sh"):
                archive.addfile(tarfile.TarInfo(name), io.BytesIO())
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            release.file_map(stream.getvalue())


class RegistryPreflight(unittest.TestCase):
    @staticmethod
    def response(value):
        return io.BytesIO(json.dumps(value).encode())

    def test_new_package_namespace_and_denied_access(self):
        missing = urllib.error.HTTPError("https://api.github.com/package", 404, "missing", {}, None)
        self.addCleanup(missing.close)
        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}),
            patch.object(
                release.urllib.request, "urlopen", side_effect=[self.response({"owner": {"type": "User"}}), missing]
            ),
        ):
            self.assertFalse(release.package_exists("Owner/Repo"))
        denied = urllib.error.HTTPError("https://api.github.com/package", 403, "denied", {}, None)
        self.addCleanup(denied.close)
        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": "test-token"}),
            patch.object(
                release.urllib.request,
                "urlopen",
                side_effect=[self.response({"owner": {"type": "Organization"}}), denied],
            ),
        ):
            with self.assertRaises(urllib.error.HTTPError):
                release.package_exists("Owner/Repo")

    def test_existing_exact_version_is_rejected(self):
        responses = [
            self.response({"owner": {"type": "User"}}),
            self.response({"name": "repo/avd"}),
            self.response({"token": "opaque"}),
            self.response({"schemaVersion": 2}),
        ]
        with (
            patch.dict(os.environ, {"GITHUB_TOKEN": "test-token", "GITHUB_ACTOR": "user"}),
            patch.object(release.urllib.request, "urlopen", side_effect=responses[:3]),
            patch.object(release.urllib.request.OpenerDirector, "open", return_value=responses[3]),
        ):
            with self.assertRaisesRegex(ValueError, "already exists"):
                release.preflight("Owner/Repo", "1.0.0")


class RegistryVerification(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.artifacts = Path(temporary.name)
        self.enterContext(patch.object(release, "ARTIFACTS", self.artifacts))
        self.enterContext(patch.dict(os.environ, {"GITHUB_OUTPUT": ""}))
        self.metadata = json.loads((release.ROOT / "src/avd/devcontainer-feature.json").read_text())
        self.metadata["version"] = "1.0.0"
        self.version = self.metadata["version"]
        self.archive = self.feature_archive()
        (self.artifacts / "devcontainer-feature-avd.tgz").write_bytes(self.archive)
        (self.artifacts / "provenance.json").write_text(
            json.dumps({"commit": "a" * 40, "feature_version": self.version})
        )
        self.responses = {}
        self.configure_manifest(self.metadata, self.archive)
        self.responses["tags/list?n=100"] = json.dumps({"tags": [self.version]}).encode()

    def feature_archive(self, content=b"tested", mode=0o755):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            for name, data, permissions in [
                ("./devcontainer-feature.json", json.dumps(self.metadata).encode(), 0o644),
                ("./install.sh", content, mode),
            ]:
                member = tarfile.TarInfo(name)
                member.mode, member.size = permissions, len(data)
                archive.addfile(member, io.BytesIO(data))
        return stream.getvalue()

    def configure_manifest(self, metadata, blob):
        layer_digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        manifest = json.dumps(
            {"layers": [{"digest": layer_digest}], "annotations": {"dev.containers.metadata": json.dumps(metadata)}}
        ).encode()
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        self.responses.update(
            {
                f"manifests/{metadata['version']}": manifest,
                f"manifests/{digest}": manifest,
                f"blobs/{layer_digest}": blob,
            }
        )
        for alias in (metadata["version"].split(".")[0], ".".join(metadata["version"].split(".")[:2]), "latest"):
            self.responses[f"manifests/{alias}"] = manifest

    def fetch(self, path, resource, token):
        self.assertEqual(path, "owner/repo/avd")
        if resource.startswith("tags/list?") and "last=" in resource:
            return b'{"tags": []}'
        return self.responses[resource]

    def verify(self):
        with (
            patch.object(
                release, "access_token", side_effect=lambda path, anonymous=False: "public" if anonymous else "private"
            ),
            patch.object(release, "fetch", side_effect=self.fetch) as fetch,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            release.verify("Owner/Repo", self.version)
        return fetch

    def test_tested_payload_metadata_and_anonymous_access_are_verified(self):
        fetch = self.verify()
        result = json.loads((self.artifacts / "published.json").read_text())
        self.assertEqual(result["status"], "payload and anonymous access verified")
        self.assertEqual(result["commit"], "a" * 40)
        self.assertTrue(any(call.args[-1] == "public" for call in fetch.call_args_list))

    def test_altered_lifecycle_options_and_environment_are_rejected(self):
        for key, value in [
            ("postCreateCommand", "echo untested"),
            ("containerEnv", {"PATH": "/untested"}),
            ("options", {}),
        ]:
            with self.subTest(key=key):
                metadata = copy.deepcopy(self.metadata)
                metadata[key] = value
                self.configure_manifest(metadata, self.archive)
                with self.assertRaisesRegex(AssertionError, "metadata differs"):
                    self.verify()
                self.assertIn("pending", json.loads((self.artifacts / "published.json").read_text())["status"])

    def test_wrong_alias_payload_content_and_modes_are_rejected(self):
        self.responses["manifests/latest"] = b"not a manifest"
        with self.assertRaisesRegex(AssertionError, "Alias latest"):
            self.verify()
        for content, mode in [(b"untested", 0o755), (b"tested", 0o644)]:
            with self.subTest(content=content, mode=mode):
                self.configure_manifest(self.metadata, self.feature_archive(content, mode))
                with self.assertRaisesRegex(AssertionError, "content or modes"):
                    self.verify()

    def test_layer_digest_and_immutable_identity_are_checked(self):
        resource = next(key for key in self.responses if key.startswith("blobs/"))
        self.responses[resource] = b"corrupted download"
        with self.assertRaises(AssertionError):
            self.verify()
        self.configure_manifest(self.metadata, self.archive)
        resource = next(key for key in self.responses if key.startswith("manifests/sha256:"))
        self.responses[resource] = b"different immutable manifest"
        with self.assertRaises(AssertionError):
            self.verify()

    def test_backport_aliases_can_point_to_newer_exact_versions(self):
        for version in ("1.0.1", "1.1.0", "2.0.0"):
            metadata = {**self.metadata, "version": version}
            self.configure_manifest(metadata, self.archive)
        # The minor alias remains at the latest patch of that minor series.
        self.responses["manifests/1.0"] = self.responses["manifests/1.0.1"]
        self.responses["tags/list?n=100"] = json.dumps({"tags": ["1.0.0", "1.0.1", "1.1.0", "2.0.0"]}).encode()
        self.verify()

    def test_tag_pagination_continues_after_short_pages_and_rejects_cycles(self):
        pages = [b'{"tags": ["1.0.0"]}', b'{"tags": ["1.10.0", "1.2.0", "2.0.0rc1"]}', b'{"tags": []}']
        with patch.object(release, "fetch", side_effect=pages) as fetch:
            targets = release.alias_targets("owner/repo/avd", "private", "1.0.0")
            self.assertEqual(targets, {"1": "1.10.0", "1.0": "1.0.0", "latest": "1.10.0"})
            self.assertEqual(
                [call.args[1] for call in fetch.call_args_list],
                ["tags/list?n=100", "tags/list?n=100&last=1.0.0", "tags/list?n=100&last=2.0.0rc1"],
            )
        with (
            patch.object(release, "fetch", return_value=pages[0]),
            self.assertRaisesRegex(ValueError, "did not advance"),
        ):
            release.alias_targets("owner/repo/avd", "private", "1.0.0")


class RegistryDownloads(unittest.TestCase):
    def test_redirects_strip_authentication_and_require_https(self):
        for location in ("https://cdn.example.test/signed?signature=test", "http://cdn.example.test/blob"):
            with self.subTest(location=location):
                requests = []

                def serve(request, *, requests=requests, location=location):
                    requests.append(request)
                    headers = email.message.Message()
                    if len(requests) == 1:
                        headers["Location"] = location
                        response = urllib.response.addinfourl(io.BytesIO(), headers, request.full_url, 307)
                        response.msg = "Temporary Redirect"
                    else:
                        response = urllib.response.addinfourl(io.BytesIO(b"payload"), headers, request.full_url, 200)
                        response.msg = "OK"
                    return response

                # Keep urllib's real redirect/error machinery; replace only the
                # HTTP(S) transport so removing NoRedirect would leak the token.
                with (
                    patch.object(release.urllib.request.HTTPSHandler, "https_open", side_effect=serve),
                    patch.object(release.urllib.request.HTTPHandler, "http_open", side_effect=serve),
                ):
                    if location.startswith("https:"):
                        self.assertEqual(release.fetch("owner/repo/avd", "blobs/sha256:123", "private"), b"payload")
                        self.assertEqual(len(requests), 2)
                        self.assertEqual(requests[1].full_url, location)
                        self.assertFalse(requests[1].has_header("Authorization"))
                    else:
                        with self.assertRaisesRegex(ValueError, "non-HTTPS"):
                            release.fetch("owner/repo/avd", "blobs/sha256:123", "private")
                        self.assertEqual(len(requests), 1)
                    self.assertEqual(requests[0].get_header("Authorization"), "Bearer private")


if __name__ == "__main__":
    unittest.main()
