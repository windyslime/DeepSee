#!/usr/bin/env python3
"""Apply the managed DeepSee DSV layer to one DSH profile."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit


START_MARKER = "# >>> deepsee-dsv managed layer >>>"
END_MARKER = "# <<< deepsee-dsv managed layer <<<"
WEB_FRONTEND = "@deepseek-ai/dsh-web-frontend"
DEFAULT_GATEWAY_URL = "http://127.0.0.1:8712"
DEFAULT_API_KEY_REF = "DEEPSEE_DSV_API_KEY"
API_KEY_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
LLM_DSV_ROW = re.compile(r"^\s*-\s+id:\s*llm-dsv\s*$", re.MULTILINE)
MANAGED_BLOCK = re.compile(
    rf"^{re.escape(START_MARKER)}\n.*?^{re.escape(END_MARKER)}\n?",
    re.MULTILINE | re.DOTALL,
)


class ProfileError(ValueError):
    """Raised when a DSH profile cannot be safely changed."""


def _validate_options(gateway_url: str | None, api_key_ref: str | None) -> None:
    if gateway_url is None and api_key_ref is None:
        return
    if gateway_url is None or api_key_ref is None:
        raise ProfileError("gateway URL and API key reference must be provided together")
    parsed = urlsplit(gateway_url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise ProfileError("gateway URL must use http:// or https:// and include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ProfileError("gateway URL must not contain user info")
    if any(character.isspace() for character in gateway_url) or "#" in gateway_url:
        raise ProfileError("gateway URL must not contain whitespace or a fragment")
    if not API_KEY_REF_RE.fullmatch(api_key_ref):
        raise ProfileError("API key reference must be an uppercase environment name")


def _patch_block(gateway_url: str | None, api_key_ref: str | None) -> str:
    block = (
        f"{START_MARKER}\n"
        "- insert:\n"
        "    - id: llm-dsv\n"
        "      name: '@deepseek-ai/dsh-llm-dsv'\n"
    )
    if gateway_url is not None and api_key_ref is not None:
        block += (
            "      config:\n"
            f"        baseURL: {gateway_url}\n"
            f"        apiKeyEnv: {api_key_ref}\n"
        )
    return f"{block}{END_MARKER}\n"


def _load_manifest(asset_root: Path) -> dict[str, Any]:
    manifest_path = asset_root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"cannot read asset manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("packages"), list):
        raise ProfileError("asset manifest has no package list")
    return manifest


def _asset_dependencies(asset_root: Path, manifest: dict[str, Any]) -> dict[str, str]:
    dependencies: dict[str, str] = {}
    for entry in manifest["packages"]:
        if not isinstance(entry, dict):
            raise ProfileError("asset manifest contains a non-object package")
        name = entry.get("name")
        relative = entry.get("path")
        if not isinstance(name, str) or not isinstance(relative, str):
            raise ProfileError("asset package entries require name and path")
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or not relative.startswith("packages/"):
            raise ProfileError(f"unsafe package path in manifest: {relative}")
        package_path = asset_root / Path(relative)
        if not package_path.is_file():
            raise ProfileError(f"asset package is missing: {relative}")
        dependencies[name] = f"file:{package_path}"
    return dependencies


def _read_profile(profile: Path) -> tuple[Path, dict[str, Any], str]:
    package_path = profile / "package.json"
    patch_path = profile / "cordis.patch.yml"
    if not package_path.is_file():
        raise ProfileError(f"DSH profile package.json is missing: {package_path}")
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError(f"cannot read profile package.json: {package_path}") from exc
    if not isinstance(package, dict):
        raise ProfileError("profile package.json must contain an object")
    patch = patch_path.read_text(encoding="utf-8") if patch_path.exists() else ""
    return package_path, package, patch


def _patched_text(
    patch: str,
    installing: bool,
    gateway_url: str | None = None,
    api_key_ref: str | None = None,
) -> str:
    if installing:
        block = _patch_block(gateway_url, api_key_ref)
        if START_MARKER in patch:
            if gateway_url is None and api_key_ref is None:
                return patch
            return MANAGED_BLOCK.sub(block, patch)
        if LLM_DSV_ROW.search(patch):
            return patch
        if patch.strip() == "[]":
            return block
        prefix = "" if not patch or patch.endswith("\n") else "\n"
        return f"{patch}{prefix}{block}"
    next_patch = MANAGED_BLOCK.sub("", patch)
    if not next_patch.strip() or all(
        not line.strip() or line.lstrip().startswith("#") for line in next_patch.splitlines()
    ):
        return "[]\n"
    return next_patch


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(f"{path.suffix}.deepsee-tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _write_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(f"{path.suffix}.deepsee-tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _result(action: str, changed: bool, manifest: dict[str, Any], profile: Path) -> None:
    print(json.dumps({
        "ok": True,
        "action": action,
        "changed": changed,
        "profile": str(profile),
        "version": manifest.get("releaseVersion"),
    }, ensure_ascii=False))


def install(
    profile: Path,
    asset_root: Path,
    dry_run: bool,
    gateway_url: str | None = None,
    api_key_ref: str | None = None,
) -> None:
    _validate_options(gateway_url, api_key_ref)
    package_path, package, patch = _read_profile(profile)
    manifest = _load_manifest(asset_root)
    dependencies = _asset_dependencies(asset_root, manifest)
    current_dependencies = package.setdefault("dependencies", {})
    if not isinstance(current_dependencies, dict):
        raise ProfileError("profile dependencies must be an object")
    changed = any(current_dependencies.get(name) != value for name, value in dependencies.items())
    if WEB_FRONTEND in dependencies:
        package_manager = package.setdefault("pnpm", {})
        if not isinstance(package_manager, dict):
            raise ProfileError("profile pnpm configuration must be an object")
        overrides = package_manager.setdefault("overrides", {})
        if not isinstance(overrides, dict):
            raise ProfileError("profile pnpm overrides must be an object")
        changed = changed or overrides.get(WEB_FRONTEND) != dependencies[WEB_FRONTEND]
        if not dry_run:
            overrides[WEB_FRONTEND] = dependencies[WEB_FRONTEND]
    next_patch = _patched_text(patch, installing=True, gateway_url=gateway_url, api_key_ref=api_key_ref)
    changed = changed or next_patch != patch
    if not dry_run:
        for name, value in dependencies.items():
            current_dependencies[name] = value
        _write_json(package_path, package)
        _write_text(profile / "cordis.patch.yml", next_patch)
    _result("install", changed, manifest, profile)


def uninstall(profile: Path, asset_root: Path, dry_run: bool) -> None:
    package_path, package, patch = _read_profile(profile)
    manifest = _load_manifest(asset_root)
    dependencies = _asset_dependencies(asset_root, manifest)
    current_dependencies = package.get("dependencies", {})
    if not isinstance(current_dependencies, dict):
        raise ProfileError("profile dependencies must be an object")
    package_manager = package.get("pnpm", {})
    if not isinstance(package_manager, dict):
        raise ProfileError("profile pnpm configuration must be an object")
    overrides = package_manager.get("overrides", {})
    if not isinstance(overrides, dict):
        raise ProfileError("profile pnpm overrides must be an object")
    changed = False
    for name, expected in dependencies.items():
        if current_dependencies.get(name) == expected:
            changed = True
            if not dry_run:
                del current_dependencies[name]
    if WEB_FRONTEND in dependencies and overrides.get(WEB_FRONTEND) == dependencies[WEB_FRONTEND]:
        changed = True
        if not dry_run:
            del overrides[WEB_FRONTEND]
            if not overrides:
                del package_manager["overrides"]
            if not package_manager:
                package.pop("pnpm", None)
    next_patch = _patched_text(patch, installing=False)
    changed = changed or next_patch != patch
    if not dry_run:
        _write_json(package_path, package)
        if next_patch:
            _write_text(profile / "cordis.patch.yml", next_patch)
    _result("uninstall", changed, manifest, profile)


def verify(profile: Path, asset_root: Path) -> None:
    _, package, patch = _read_profile(profile)
    manifest = _load_manifest(asset_root)
    dependencies = _asset_dependencies(asset_root, manifest)
    current_dependencies = package.get("dependencies", {})
    if not isinstance(current_dependencies, dict):
        raise ProfileError("profile dependencies must be an object")
    missing = [name for name, value in dependencies.items() if current_dependencies.get(name) != value]
    if missing:
        raise ProfileError(f"profile is missing managed dependencies: {', '.join(missing)}")
    if not (START_MARKER in patch or LLM_DSV_ROW.search(patch)):
        raise ProfileError("profile patch does not contain the llm-dsv row")
    _result("verify", False, manifest, profile)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall", "verify"))
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--asset-root", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--gateway-url")
    parser.add_argument("--api-key-ref")
    args = parser.parse_args(argv)
    try:
        if args.action == "install":
            install(
                args.profile,
                args.asset_root,
                args.dry_run,
                gateway_url=args.gateway_url,
                api_key_ref=args.api_key_ref,
            )
        elif args.action == "uninstall":
            uninstall(args.profile, args.asset_root, args.dry_run)
        else:
            verify(args.profile, args.asset_root)
    except (OSError, ProfileError) as exc:
        print(f"profile operation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
