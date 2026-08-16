from __future__ import annotations

import io
import json
import subprocess
import tarfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
PROFILE_TOOL = ROOT / "scripts" / "dsh-profile.py"


def _asset_root(tmp_path: Path) -> Path:
    root = tmp_path / "asset"
    packages = root / "packages"
    packages.mkdir(parents=True)
    names = [
        "@deepseek-ai/dsh-llm-dsv",
        "@deepseek-ai/dsh-session",
    ]
    entries = []
    for name in names:
        filename = f"{name.split('/')[-1]}-0.1.0-rc.5.tgz"
        package_path = packages / filename
        with tarfile.open(package_path, "w:gz") as archive:
            payload = json.dumps({"name": name, "version": "0.1.0-rc.5"}).encode()
            info = tarfile.TarInfo("package/package.json")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        entries.append({"name": name, "version": "0.1.0-rc.5", "path": f"packages/{filename}"})
    (root / "manifest.json").write_text(
        json.dumps({"releaseVersion": "0.1.0", "packages": entries}), encoding="utf-8"
    )
    return root


def _run(action: str, profile: Path, asset_root: Path, dry_run: bool = False) -> subprocess.CompletedProcess[str]:
    command = [
        "python3",
        str(PROFILE_TOOL),
        action,
        "--profile",
        str(profile),
        "--asset-root",
        str(asset_root),
    ]
    if dry_run:
        command.append("--dry-run")
    return subprocess.run(command, text=True, capture_output=True, check=False)


def _profile(tmp_path: Path) -> Path:
    profile = tmp_path / "profiles" / "web"
    profile.mkdir(parents=True)
    (profile / "package.json").write_text(json.dumps({
        "name": "dsh-profile-web",
        "dependencies": {"unrelated": "1.0.0"},
        "dsh": {"profile": {"bundles": ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-web-app"]}},
    }), encoding="utf-8")
    (profile / "cordis.patch.yml").write_text("- id: unrelated\n  name: example\n", encoding="utf-8")
    return profile


def test_install_is_idempotent_and_preserves_existing_patch(tmp_path: Path):
    profile = _profile(tmp_path)
    asset_root = _asset_root(tmp_path)
    first = _run("install", profile, asset_root)
    second = _run("install", profile, asset_root)
    assert first.returncode == second.returncode == 0, (first.stderr, second.stderr)
    package = json.loads((profile / "package.json").read_text(encoding="utf-8"))
    assert package["dependencies"]["unrelated"] == "1.0.0"
    assert package["dependencies"]["@deepseek-ai/dsh-llm-dsv"].startswith("file:")
    patch = (profile / "cordis.patch.yml").read_text(encoding="utf-8")
    assert patch.count("deepsee-dsv managed layer") == 2
    assert patch.count("id: llm-dsv") == 1
    assert "id: unrelated" in patch


def test_dry_run_does_not_write(tmp_path: Path):
    profile = _profile(tmp_path)
    asset_root = _asset_root(tmp_path)
    before = {path: path.read_bytes() for path in profile.iterdir()}
    result = _run("install", profile, asset_root, dry_run=True)
    assert result.returncode == 0, result.stderr
    assert {path: path.read_bytes() for path in profile.iterdir()} == before


def test_verify_and_uninstall_remove_only_managed_layer(tmp_path: Path):
    profile = _profile(tmp_path)
    asset_root = _asset_root(tmp_path)
    assert _run("install", profile, asset_root).returncode == 0
    assert _run("verify", profile, asset_root).returncode == 0
    result = _run("uninstall", profile, asset_root)
    assert result.returncode == 0, result.stderr
    package = json.loads((profile / "package.json").read_text(encoding="utf-8"))
    assert "@deepseek-ai/dsh-llm-dsv" not in package["dependencies"]
    assert package["dependencies"]["unrelated"] == "1.0.0"
    patch = (profile / "cordis.patch.yml").read_text(encoding="utf-8")
    assert "id: unrelated" in patch
    assert "id: llm-dsv" not in patch


def test_uninstall_turns_comment_only_patch_into_empty_list(tmp_path: Path):
    profile = _profile(tmp_path)
    (profile / "cordis.patch.yml").write_text("# only a comment\n", encoding="utf-8")
    asset_root = _asset_root(tmp_path)
    assert _run("install", profile, asset_root).returncode == 0
    assert _run("uninstall", profile, asset_root).returncode == 0
    assert (profile / "cordis.patch.yml").read_text(encoding="utf-8") == "[]\n"


def test_missing_profile_fails_without_creating_files(tmp_path: Path):
    asset_root = _asset_root(tmp_path)
    profile = tmp_path / "missing"
    result = _run("install", profile, asset_root)
    assert result.returncode == 1
    assert not profile.exists()
