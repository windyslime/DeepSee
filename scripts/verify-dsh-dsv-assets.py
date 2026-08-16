#!/usr/bin/env python3
"""Verify a DeepSee DSH DSV asset archive without installing it."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import posixpath
import sys
import tarfile
from pathlib import PurePosixPath
from typing import Any


class AssetError(ValueError):
    """Raised when a release asset is unsafe or inconsistent."""


ALLOWED_PACKAGES = {
    "@deepseek-ai/dsh-llm-dsv",
    "@deepseek-ai/dsh-session",
    "@deepseek-ai/dsh-client-runtime",
    "@deepseek-ai/dsh-client-ui-conversation",
    "@deepseek-ai/dsh-client-ui-primitives",
    "@deepseek-ai/dsh-client-ui-tool",
    "@deepseek-ai/dsh-client-ui-trajectory",
    "@deepseek-ai/dsh-cordis-client-runner",
    "@deepseek-ai/dsh-web-app",
    "@deepseek-ai/dsh-web-frontend",
}


def _read_json(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except json.JSONDecodeError as exc:
        raise AssetError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AssetError(f"{label} must contain a JSON object")
    return value


def _safe_member(name: str) -> None:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts:
        raise AssetError(f"unsafe archive member: {name!r}")
    if "\\" in name or name.startswith("/"):
        raise AssetError(f"unsafe archive member: {name!r}")


def _package_json(package_bytes: bytes, expected_name: str) -> dict[str, Any]:
    try:
        with tarfile.open(fileobj=io.BytesIO(package_bytes), mode="r:gz") as package_tar:
            members = package_tar.getmembers()
            for member in members:
                _safe_member(member.name)
                if member.issym() or member.islnk():
                    raise AssetError(f"package contains a link: {member.name}")
            manifest_member = package_tar.getmember("package/package.json")
            manifest_data = package_tar.extractfile(manifest_member)
            if manifest_data is None:
                raise AssetError(f"{expected_name} has no package/package.json")
            package = _read_json(manifest_data.read(), f"{expected_name}/package.json")
    except (tarfile.TarError, KeyError) as exc:
        raise AssetError(f"invalid package archive for {expected_name}: {exc}") from exc

    if package.get("name") != expected_name:
        raise AssetError(
            f"package name mismatch: expected {expected_name}, got {package.get('name')!r}"
        )
    serialized = json.dumps(package, ensure_ascii=False)
    for marker in ("/Users/", "/home/", "\\Users\\"):
        if marker in serialized:
            raise AssetError(f"{expected_name} contains a development path")
    return package


def verify_archive(archive_path: str, manifest_path: str | None = None) -> dict[str, Any]:
    with tarfile.open(archive_path, mode="r:gz") as archive:
        members = archive.getmembers()
        for member in members:
            _safe_member(member.name)
            if member.issym() or member.islnk():
                raise AssetError(f"asset archive contains a link: {member.name}")

        try:
            archive_manifest_member = archive.getmember("manifest.json")
        except KeyError as exc:
            raise AssetError("asset archive has no manifest.json") from exc
        manifest_stream = archive.extractfile(archive_manifest_member)
        if manifest_stream is None:
            raise AssetError("manifest.json cannot be read")
        manifest_bytes = manifest_stream.read()
        manifest = _read_json(manifest_bytes, "manifest.json")

        if manifest_path:
            with open(manifest_path, "rb") as sidecar:
                sidecar_manifest = _read_json(sidecar.read(), manifest_path)
            if sidecar_manifest != manifest:
                raise AssetError("sidecar manifest does not match archive manifest")

        if manifest.get("schemaVersion") != 1:
            raise AssetError("unsupported manifest schema")
        packages = manifest.get("packages")
        if not isinstance(packages, list) or not packages:
            raise AssetError("manifest packages must be a non-empty list")

        declared: set[str] = {"manifest.json"}
        seen_names: set[str] = set()
        for entry in packages:
            if not isinstance(entry, dict):
                raise AssetError("each manifest package must be an object")
            name = entry.get("name")
            path = entry.get("path")
            digest = entry.get("sha256")
            if not all(isinstance(value, str) and value for value in (name, path, digest)):
                raise AssetError("package entries require name, path, and sha256")
            if name not in ALLOWED_PACKAGES:
                raise AssetError(f"unknown package name: {name}")
            if name in seen_names:
                raise AssetError(f"duplicate package name: {name}")
            seen_names.add(name)
            _safe_member(path)
            if path in declared:
                raise AssetError(f"duplicate archive path: {path}")
            declared.add(path)
            try:
                member = archive.getmember(path)
            except KeyError as exc:
                raise AssetError(f"manifest package is missing: {path}") from exc
            package_stream = archive.extractfile(member)
            if package_stream is None:
                raise AssetError(f"manifest package cannot be read: {path}")
            package_bytes = package_stream.read()
            actual_digest = hashlib.sha256(package_bytes).hexdigest()
            if actual_digest != digest:
                raise AssetError(f"checksum mismatch for {path}")
            package = _package_json(package_bytes, name)
            expected_version = entry.get("version")
            if expected_version is not None and package.get("version") != expected_version:
                raise AssetError(f"version mismatch for {name}")

        actual = {member.name for member in members}
        if actual != declared:
            unexpected = sorted(actual - declared)
            missing = sorted(declared - actual)
            raise AssetError(f"archive members differ from manifest; unexpected={unexpected}, missing={missing}")
        return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("manifest", nargs="?")
    args = parser.parse_args(argv)
    try:
        manifest = verify_archive(args.archive, args.manifest)
    except (OSError, AssetError, tarfile.TarError) as exc:
        print(f"asset verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, "version": manifest.get("releaseVersion"), "packages": len(manifest["packages"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
