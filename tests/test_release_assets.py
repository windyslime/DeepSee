from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "verify-dsh-dsv-assets.py"
SPEC = importlib.util.spec_from_file_location("dsv_asset_verifier", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _package(name: str, version: str = "0.1.0-rc.5") -> bytes:
    payload = json.dumps({"name": name, "version": version}).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _archive(
    tmp_path: Path,
    package_name: str = "@deepseek-ai/dsh-llm-dsv",
    unsafe=False,
    wrong_digest=False,
):
    package_path = "packages/dsh-llm-dsv-0.1.0-rc.5.tgz"
    package_bytes = _package(package_name)
    manifest = {
        "schemaVersion": 1,
        "releaseVersion": "0.1.0",
        "packages": [{
            "name": package_name,
            "version": "0.1.0-rc.5",
            "path": "../escape.tgz" if unsafe else package_path,
            "sha256": "0" * 64 if wrong_digest else hashlib.sha256(package_bytes).hexdigest(),
        }],
    }
    archive_path = tmp_path / "assets.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        manifest_bytes = json.dumps(manifest).encode()
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_bytes)
        archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        package_info = tarfile.TarInfo(package_path)
        package_info.size = len(package_bytes)
        archive.addfile(package_info, io.BytesIO(package_bytes))
    return archive_path


def test_verifier_accepts_matching_archive(tmp_path: Path):
    assert MODULE.verify_archive(str(_archive(tmp_path)))['releaseVersion'] == "0.1.0"


def test_verifier_rejects_path_traversal(tmp_path: Path):
    with pytest.raises(MODULE.AssetError, match="unsafe"):
        MODULE.verify_archive(str(_archive(tmp_path, unsafe=True)))


def test_verifier_rejects_checksum_mismatch(tmp_path: Path):
    archive_path = _archive(tmp_path, wrong_digest=True)
    with pytest.raises((MODULE.AssetError, tarfile.TarError)):
        MODULE.verify_archive(str(archive_path))


def test_verifier_rejects_unknown_package_name(tmp_path: Path):
    with pytest.raises(MODULE.AssetError, match="unknown package name"):
        MODULE.verify_archive(str(_archive(tmp_path, "@unknown/package")))
