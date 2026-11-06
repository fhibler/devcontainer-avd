"""Verified download inputs shared by CI and the normal Feature installer."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import tarfile
from pathlib import Path

DEFAULT_ROOT = Path("/opt/avd/download-cache")


def root():
    value = os.environ.get("AVD_DOWNLOAD_CACHE", str(DEFAULT_ROOT))
    return Path(value) if value else None


def recording():
    value = os.environ.get("AVD_CACHE_RECORD")
    return Path(value) if value else None


def today():
    return datetime.datetime.now(datetime.UTC).date().isoformat()


def key(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    temporary.replace(path)


def checked_path(directory, name):
    path = directory / name
    if not name or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("Unsafe download-cache path")
    if not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("Download-cache link escapes its root")
    return path


def metadata(url):
    directory = root()
    if directory is None:
        return None
    path = directory / "metadata" / (key(url) + ".json")
    if not path.is_file():
        return None
    entry = json.loads(path.read_text())
    if entry["url"] != url:
        raise ValueError("Cached metadata URL does not match its key")
    return entry["data"] if entry["epoch"] == today() else None


def record_metadata(url, data):
    directory = recording()
    if directory is not None:
        write_json(directory / "metadata" / (key(url) + ".json"), {"url": url, "epoch": today(), "data": data})


def collection_key(requirements, controller):
    return key({"requirements": requirements, "ansible": controller})


def collections(requirements, controller):
    directory = root()
    if directory is None:
        return None
    directory = directory / "galaxy" / collection_key(requirements, controller)
    manifest = directory / "manifest.json"
    if not manifest.is_file():
        return None
    entry = json.loads(manifest.read_text())
    if entry["requirements"] != requirements or entry["ansible"] != controller:
        raise ValueError("Cached Galaxy requirements do not match their key")
    if entry["epoch"] != today():
        return None
    archives = []
    for item in entry["archives"]:
        path = checked_path(directory, item["name"])
        if path.suffix != ".gz" or not path.is_file() or sha256(path) != item["sha256"]:
            raise ValueError("Cached Galaxy archive checksum mismatch")
        archives.append(path)
    if not archives:
        raise ValueError("Cached Galaxy resolution contains no archives")
    return archives


def source(repository, commit, *, directory=None):
    directory = directory or root()
    if directory is None or len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        return None
    manifest = directory / "sources" / (commit + ".json")
    if not manifest.is_file():
        return None
    entry = json.loads(manifest.read_text())
    if entry["repository"] != repository or entry["commit"] != commit:
        return None
    archive = directory / "sources" / (commit + ".tgz")
    if not archive.is_file() or sha256(archive) != entry["sha256"]:
        raise ValueError("Cached source archive checksum mismatch")
    return archive


def extract(archive, destination):
    with tarfile.open(archive) as stream:
        stream.extractall(destination, filter="data")


def copy_tree(source, destination):
    """Merge inputs while replacing existing symlinks without following them."""
    source, destination = Path(source), Path(destination)
    if destination.is_symlink():
        destination.unlink()
    destination.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        target = destination / path.name
        if path.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink(missing_ok=True)
            target.symlink_to(path.readlink())
        elif path.is_dir():
            if target.is_file() or target.is_symlink():
                target.unlink()
            copy_tree(path, target)
        else:
            if target.is_symlink():
                target.unlink()
            shutil.copy2(path, target)


def seed_uv(destination):
    directory = root()
    destination = Path(destination)
    if directory is None or not (directory / "uv").is_dir() or destination.resolve() == (directory / "uv").resolve():
        return
    identity = directory / "SHA256SUMS"
    if not identity.is_file():
        raise ValueError("Download cache is missing its integrity index")
    stamp = sha256(identity)
    marker = destination / ".avd-download-seed"
    if marker.is_file() and marker.read_text().strip() == stamp:
        return
    copy_tree(directory / "uv", destination)
    marker.write_text(stamp + "\n")


def files(directory):
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            checked_path(directory, str(path.relative_to(directory)))
        if path.is_file() and path != directory / "SHA256SUMS":
            name = str(path.relative_to(directory))
            if any(char in name for char in ("\n", "\r", "\\")):
                raise ValueError("Unsupported download-cache filename")
            yield name, path


def links(directory):
    return {
        str(path.relative_to(directory)): str(path.readlink())
        for path in sorted(directory.rglob("*"))
        if path.is_symlink()
    }


def write_checksums(directory):
    directory = Path(directory)
    write_json(directory / "LINKS.json", links(directory))
    (directory / "SHA256SUMS").write_text("".join(f"{sha256(path)}  {name}\n" for name, path in files(directory)))


def verify(directory):
    directory = Path(directory)
    expected = {}
    for line in (directory / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        if name in expected:
            raise ValueError("Duplicate download-cache integrity entry")
        expected[name] = digest
    actual = {name: sha256(checked_path(directory, name)) for name, _ in files(directory)}
    if actual != expected:
        raise ValueError("Download-cache integrity check failed")
    if json.loads((directory / "LINKS.json").read_text()) != links(directory):
        raise ValueError("Download-cache link integrity check failed")


def pack(directory, archive):
    directory, archive = Path(directory), Path(archive)
    write_checksums(directory)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "w:gz", compresslevel=1) as stream:
        stream.add(directory, arcname=".")
    archive.with_suffix(archive.suffix + ".sha256").write_text(sha256(archive) + "\n")


def unpack(archive, destination):
    archive = Path(archive)
    expected = archive.with_suffix(archive.suffix + ".sha256").read_text().strip()
    if sha256(archive) != expected:
        raise ValueError("Download bundle checksum mismatch")
    extract(archive, destination)
    verify(destination)
