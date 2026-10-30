#!/usr/bin/env python3
"""GHCR release preflight and immutable published-payload verification."""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import re
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts"
ACCEPT = "application/vnd.oci.image.manifest.v1+json"


def registry_path(repository):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Expected GitHub owner/repository identity")
    return repository.lower() + "/avd"


def access_token(path, anonymous=False):
    headers = {}
    if not anonymous:
        credentials = f"{os.environ['GITHUB_ACTOR']}:{os.environ['GITHUB_TOKEN']}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(credentials).decode()
    url = "https://ghcr.io/token?" + urllib.parse.urlencode({"service": "ghcr.io", "scope": f"repository:{path}:pull"})
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        return json.load(response)["token"]


def fetch(path, resource, token):
    headers = {"Authorization": "Bearer " + token, "Accept": ACCEPT}
    # Never forward authorization through a registry redirect (blob CDN URLs are
    # signed independently). urllib's default redirect handler copies headers.
    opener = urllib.request.build_opener(NoRedirect)
    url = f"https://ghcr.io/v2/{path}/{resource}"
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            with exc:
                location = exc.headers["Location"]
                if urllib.parse.urlsplit(location).scheme != "https":
                    raise ValueError("Registry returned a non-HTTPS blob redirect") from None
            with urllib.request.urlopen(location, timeout=60) as response:
                return response.read()
        raise


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def file_map(data):
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        result = {}
        for member in archive.getmembers():
            if member.isdir():
                continue
            if not member.isfile() or Path(member.name).is_absolute() or ".." in Path(member.name).parts:
                raise ValueError("Unexpected unsafe archive member")
            name = str(Path(member.name))
            if name in result:
                raise ValueError("Duplicate archive member")
            result[name] = {
                "mode": member.mode & 0o777,
                "sha256": hashlib.sha256(archive.extractfile(member).read()).hexdigest(),
            }
        return result


def package_exists(repository):
    """GitHub distinguishes a new namespace from a denied registry token."""
    owner, repo = repository.split("/")
    headers = {
        "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    def request(path):
        with urllib.request.urlopen(
            urllib.request.Request("https://api.github.com" + path, headers=headers), timeout=30
        ) as response:
            return json.load(response)

    kind = request(f"/repos/{repository}")["owner"]["type"]
    prefix = "orgs" if kind == "Organization" else "users"
    name = urllib.parse.quote(repo.lower() + "/avd", safe="")
    try:
        request(f"/{prefix}/{owner}/packages/container/{name}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise
    return True


def preflight(repository, version):
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("Feature version must be stable semantic X.Y.Z")
    path = registry_path(repository)
    if not package_exists(repository):
        print(f"New package namespace ghcr.io/{path}; exact version is unpublished")
        return
    token = access_token(path)
    try:
        fetch(path, f"manifests/{version}", token)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"ghcr.io/{path}:{version} is available for first publication")
            return
        raise
    raise ValueError(f"Exact version ghcr.io/{path}:{version} already exists; never overwrite it")


def alias_targets(path, token, version):
    """Match the publisher's semantic aliases, including backport releases."""
    tags = set()
    resource = "tags/list?n=100"
    while True:
        page = json.loads(fetch(path, resource, token))["tags"] or []
        if not page:
            break
        if tags.intersection(page):
            raise ValueError("Registry tag pagination did not advance")
        tags.update(page)
        resource = "tags/list?" + urllib.parse.urlencode({"n": 100, "last": page[-1]})
    stable = {
        tag: tuple(map(int, tag.split(".")))
        for tag in tags
        if re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", tag)
    }
    if version not in stable:
        raise ValueError("Published version is missing from registry tags")
    major, minor, _ = version.split(".")
    return {
        alias: max((tag for tag in stable if alias == "latest" or tag.startswith(alias + ".")), key=stable.__getitem__)
        for alias in (major, f"{major}.{minor}", "latest")
    }


def verify(repository, version):
    path = registry_path(repository)
    token = access_token(path)
    manifest_bytes = fetch(path, f"manifests/{version}", token)
    digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()
    reference = f"ghcr.io/{path}@{digest}"
    provenance = json.loads((ARTIFACTS / "provenance.json").read_text())
    published = {
        "reference": reference,
        "digest": digest,
        "version": version,
        "commit": provenance["commit"],
        "status": "published; verification pending",
    }
    (ARTIFACTS / "published.json").write_text(json.dumps(published, indent=2) + "\n")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as handle:
            handle.write(f"reference={reference}\ndigest={digest}\n")
    published["aliases"] = {}
    for alias, target in alias_targets(path, token, version).items():
        payload = fetch(path, f"manifests/{alias}", token)
        published["aliases"][alias] = "sha256:" + hashlib.sha256(payload).hexdigest()
        expected = manifest_bytes if target == version else fetch(path, f"manifests/{target}", token)
        assert payload == expected, f"Alias {alias} must point to {target}"
        alias_metadata = json.loads(json.loads(payload)["annotations"]["dev.containers.metadata"])
        assert alias_metadata["id"] == "avd" and alias_metadata["version"] == target
    (ARTIFACTS / "published.json").write_text(json.dumps(published, indent=2) + "\n")
    # Fetch the immutable identity, rather than trusting a mutable version tag.
    immutable = fetch(path, f"manifests/{digest}", token)
    assert immutable == manifest_bytes
    manifest = json.loads(immutable)
    assert len(manifest["layers"]) == 1, "Expected one Feature payload layer"
    feature_metadata = json.loads(manifest["annotations"]["dev.containers.metadata"])
    tested = (ARTIFACTS / "devcontainer-feature-avd.tgz").read_bytes()
    tested_files = file_map(tested)
    with tarfile.open(fileobj=io.BytesIO(tested), mode="r:*") as archive:
        member = next(member for member in archive if str(Path(member.name)) == "devcontainer-feature.json")
        tested_metadata = json.load(archive.extractfile(member))
    assert tested_metadata["id"] == "avd" and tested_metadata["version"] == version
    assert feature_metadata == tested_metadata, "Published Feature metadata differs from the tested payload"
    layer = manifest["layers"][0]
    blob = fetch(path, f"blobs/{layer['digest']}", token)
    assert "sha256:" + hashlib.sha256(blob).hexdigest() == layer["digest"]
    assert file_map(blob) == tested_files, "Published file content or modes differ from tested payload"
    provenance = json.loads((ARTIFACTS / "provenance.json").read_text())
    assert provenance["feature_version"] == version
    reference = f"ghcr.io/{path}@{digest}"
    # Anonymous access is mandatory for this public Feature. Visibility may need
    # a one-time owner change in GHCR; a private package must fail this check.
    public_token = access_token(path, anonymous=True)
    assert fetch(path, f"manifests/{digest}", public_token) == immutable
    published["status"] = "payload and anonymous access verified"
    (ARTIFACTS / "published.json").write_text(json.dumps(published, indent=2) + "\n")
    print(reference)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "verify"))
    parser.add_argument("repository")
    parser.add_argument("version")
    args = parser.parse_args()
    if args.command == "preflight":
        preflight(args.repository, args.version)
    else:
        verify(args.repository, args.version)


if __name__ == "__main__":
    main()
