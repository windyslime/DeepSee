from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import tarfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "install-dsh-dsv.sh"


def _package(name: str) -> bytes:
    payload = json.dumps({"name": name, "version": "0.1.0-rc.5"}).encode()
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        info = tarfile.TarInfo("package/package.json")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


def _release_root(tmp_path: Path) -> tuple[Path, Path]:
    scripts_root = tmp_path / "scripts"
    scripts_root.mkdir(exist_ok=True)
    for name in ("dsh-profile.py", "verify-dsh-dsv-assets.py", "dsh-credentials.py"):
        (scripts_root / name).write_bytes((ROOT / "scripts" / name).read_bytes())

    package_names = ("@deepseek-ai/dsh-llm-dsv", "@deepseek-ai/dsh-session")
    package_bytes = {name: _package(name) for name in package_names}
    entries = []
    for name, data in package_bytes.items():
        filename = f"{name.split('/')[-1]}-0.1.0-rc.5.tgz"
        entries.append({
            "name": name,
            "version": "0.1.0-rc.5",
            "path": f"packages/{filename}",
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    manifest = {"schemaVersion": 1, "releaseVersion": "0.1.0", "packages": entries}
    archive_bytes = io.BytesIO()
    with tarfile.open(fileobj=archive_bytes, mode="w:gz") as archive:
        manifest_bytes = json.dumps(manifest).encode()
        manifest_info = tarfile.TarInfo("manifest.json")
        manifest_info.size = len(manifest_bytes)
        archive.addfile(manifest_info, io.BytesIO(manifest_bytes))
        for entry, name in zip(entries, package_names):
            package_info = tarfile.TarInfo(entry["path"])
            package_info.size = len(package_bytes[name])
            archive.addfile(package_info, io.BytesIO(package_bytes[name]))
    release_root = tmp_path / "release" / "dsh-dsv-v0.1.0"
    release_root.mkdir(parents=True, exist_ok=True)
    asset = release_root / "deepsee-dsh-dsv-v0.1.0.tar.gz"
    asset.write_bytes(archive_bytes.getvalue())
    return scripts_root, tmp_path / "release"


def _profile(tmp_path: Path) -> tuple[Path, Path]:
    dsh_home = tmp_path / "dsh"
    profile = dsh_home / "profiles" / "web"
    profile.mkdir(parents=True, exist_ok=True)
    if not (profile / "package.json").exists():
        (profile / "package.json").write_text(
            json.dumps({"name": "dsh-profile-web", "dependencies": {}}), encoding="utf-8"
        )
    if not (profile / "cordis.patch.yml").exists():
        (profile / "cordis.patch.yml").write_text("[]\n", encoding="utf-8")
    return dsh_home, profile


def _commands(tmp_path: Path) -> Path:
    command_dir = tmp_path / "bin"
    command_dir.mkdir(exist_ok=True)
    for name, body in {
        "node": "#!/bin/sh\nexit 0\n",
        "pnpm": "#!/bin/sh\n[ \"${PNPM_FAIL:-0}\" = 1 ] && exit 7\nexit 0\n",
        "dsh": "#!/bin/sh\n[ \"${DSH_FAIL:-0}\" = 1 ] && exit 7\nexit 0\n",
    }.items():
        path = command_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(0o755)
    return command_dir


def _run(tmp_path: Path, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
    scripts_root, release_root = _release_root(tmp_path)
    dsh_home, _ = _profile(tmp_path)
    command_dir = _commands(tmp_path)
    env = os.environ.copy()
    env.update({
        "DSH_HOME": str(dsh_home),
        "DEEPSEE_INSTALLER_BASE_URL": scripts_root.as_uri(),
        "DEEPSEE_RELEASE_BASE_URL": release_root.as_uri(),
        "DEEPSEE_GATEWAY_URL": "http://127.0.0.1:9",
        "PATH": f"{command_dir}{os.pathsep}{env['PATH']}",
    })
    env.update(extra_env)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_no_configure_preserves_credentials_and_does_not_download_helper(tmp_path: Path):
    dsh_home, _ = _profile(tmp_path)
    credentials = dsh_home / ".credentials.yaml"
    original = "DEEPSEEK_API_KEY: keep-me\n"
    credentials.write_text(original, encoding="utf-8")
    credentials.chmod(0o600)

    result = _run(tmp_path, "--no-configure")

    assert result.returncode == 0, result.stderr
    assert credentials.read_text(encoding="utf-8") == original
    assert not (dsh_home / "cache/deepsee-dsv/0.1.0/dsh-credentials.py").exists()
    assert "apiKeyEnv" not in (dsh_home / "profiles/web/cordis.patch.yml").read_text()


def test_configure_writes_key_mode_and_dynamic_patch_without_logging_key(tmp_path: Path):
    dsh_home, _ = _profile(tmp_path)
    secret = "dsv-test-secret"

    result = _run(
        tmp_path,
        "--configure",
        DEEPSEE_DSV_API_KEY=secret,
        DEEPSEE_GATEWAY_URL="https://gateway.example.test/dsv",
    )

    assert result.returncode == 0, result.stderr
    credentials = dsh_home / ".credentials.yaml"
    assert f'DEEPSEE_DSV_API_KEY: "{secret}"' in credentials.read_text()
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    patch = (dsh_home / "profiles/web/cordis.patch.yml").read_text()
    assert "baseURL: https://gateway.example.test/dsv" in patch
    assert "apiKeyEnv: DEEPSEE_DSV_API_KEY" in patch
    assert secret not in result.stdout + result.stderr


def test_configure_rolls_back_credentials_and_profile_when_pnpm_fails(tmp_path: Path):
    dsh_home, profile = _profile(tmp_path)
    credentials = dsh_home / ".credentials.yaml"
    credentials.write_text("DEEPSEE_DSV_API_KEY: old-value\n", encoding="utf-8")
    credentials.chmod(0o600)
    package_before = (profile / "package.json").read_bytes()
    patch_before = (profile / "cordis.patch.yml").read_bytes()

    result = _run(tmp_path, "--configure", PNPM_FAIL="1", DEEPSEE_DSV_API_KEY="new-value")

    assert result.returncode != 0
    assert (profile / "package.json").read_bytes() == package_before
    assert (profile / "cordis.patch.yml").read_bytes() == patch_before
    assert credentials.read_text() == "DEEPSEE_DSV_API_KEY: old-value\n"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600


def test_noninteractive_install_requires_an_explicit_choice(tmp_path: Path):
    result = _run(tmp_path)

    assert result.returncode == 2
    assert "--configure" in result.stderr
    assert "--no-configure" in result.stderr


def test_verify_is_read_only_after_install(tmp_path: Path):
    first = _run(tmp_path, "--no-configure")
    assert first.returncode == 0, first.stderr
    dsh_home = tmp_path / "dsh"
    profile = dsh_home / "profiles" / "web"
    before = {
        path.name: path.read_bytes()
        for path in profile.iterdir()
        if path.is_file()
    }
    backups_before = sorted((profile / ".deepsee-dsv-backups").iterdir())
    second = _run(tmp_path, "--verify")

    assert second.returncode == 0, second.stderr
    after = {path.name: path.read_bytes() for path in profile.iterdir() if path.is_file()}
    assert after == before
    assert sorted((profile / ".deepsee-dsv-backups").iterdir()) == backups_before
    assert dsh_home.exists()
