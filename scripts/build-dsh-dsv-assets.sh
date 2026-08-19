#!/usr/bin/env bash
set -euo pipefail

usage() {
  printf 'Usage: %s <harness-repo> <output-dir> <commit>\n' "$0" >&2
}

if [[ $# -ne 3 ]]; then
  usage
  exit 2
fi

harness_repo=$1
output_dir=$2
requested_commit=$3
script_dir=$(cd "$(dirname "$0")" && pwd)
repo_root=$(cd "$script_dir/.." && pwd)
manifest_source="$script_dir/dsh-dsv-release.json"

[[ -d "$harness_repo" ]] || { printf 'harness repo not found: %s\n' "$harness_repo" >&2; exit 1; }
[[ -f "$manifest_source" ]] || { printf 'release manifest not found: %s\n' "$manifest_source" >&2; exit 1; }
command -v pnpm >/dev/null 2>&1 || { printf 'pnpm is required\n' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf 'python3 is required\n' >&2; exit 1; }

actual_commit=$(git -C "$harness_repo" rev-parse HEAD)
if [[ "$actual_commit" != "$requested_commit" ]]; then
  printf 'harness checkout mismatch: expected %s, got %s\n' "$requested_commit" "$actual_commit" >&2
  exit 1
fi

mkdir -p "$output_dir"
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/deepsee-dsv-assets.XXXXXX")
trap 'rm -rf "$work_dir"' EXIT
mkdir -p "$work_dir/packages" "$work_dir/repack"

cp "$manifest_source" "$work_dir/source-manifest.json"

python3 - "$work_dir/source-manifest.json" "$work_dir/package-sources.json" <<'PY'
import json
import sys

source, target = sys.argv[1:]
with open(source, encoding="utf-8") as handle:
    manifest = json.load(handle)
with open(target, "w", encoding="utf-8") as handle:
    json.dump(manifest["packages"], handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY

while IFS=$'\t' read -r package_name package_source rewrite_web_frontend release_version; do
  package_dir="$harness_repo/$package_source"
  [[ -f "$package_dir/package.json" ]] || { printf 'package source missing: %s\n' "$package_dir" >&2; exit 1; }
  package_json=$(cat "$package_dir/package.json")
  actual_name=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["name"])' "$package_json")
  [[ "$actual_name" == "$package_name" ]] || { printf 'package name mismatch at %s\n' "$package_source" >&2; exit 1; }
  if [[ "$rewrite_web_frontend" == 1 ]]; then
    repack_dir="$work_dir/repack/${package_name##*/}"
    mkdir -p "$repack_dir"
    tar -xzf "$(cd "$package_dir" && pnpm pack --pack-destination "$work_dir/repack" --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["filename"])')" -C "$repack_dir"
    python3 - "$repack_dir/package/package.json" "$release_version" <<'PY'
import json
import sys

path, release_version = sys.argv[1:]
with open(path, encoding="utf-8") as handle:
    package = json.load(handle)
package["version"] = release_version
package["dependencies"]["@deepseek-ai/dsh-web-frontend"] = "0.1.0-rc.5"
with open(path, "w", encoding="utf-8") as handle:
    json.dump(package, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
    (cd "$repack_dir/package" && pnpm pack --pack-destination "$work_dir/packages" >/dev/null)
  else
    (cd "$package_dir" && pnpm pack --pack-destination "$work_dir/packages" >/dev/null)
  fi
done < <(python3 - "$work_dir/source-manifest.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    manifest = json.load(handle)
for entry in manifest["packages"]:
    print("\t".join([
        entry["name"],
        entry["source"],
        "1" if entry.get("rewriteWebFrontendDependency") else "0",
        entry.get("releaseVersion", ""),
    ]))
PY
)

python3 - "$work_dir/source-manifest.json" "$work_dir" "$output_dir" "$requested_commit" "$repo_root" <<'PY'
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile

source_path, work_dir, output_dir, commit, repo_root = sys.argv[1:]
with open(source_path, encoding="utf-8") as handle:
    manifest = json.load(handle)
manifest["harnessCommit"] = commit
entries = []
package_dir = pathlib.Path(work_dir) / "packages"
for entry in manifest["packages"]:
    name = entry["name"]
    candidates = sorted(package_dir.glob(f"*{name.split('/')[-1]}-*.tgz"))
    if not candidates:
        raise SystemExit(f"no packed tarball for {name}")
    tarball = candidates[-1]
    target_name = tarball.name
    target = package_dir / target_name
    data = target.read_bytes()
    package_manifest = None
    with tarfile.open(target, "r:gz") as package_tar:
        package_manifest = json.load(package_tar.extractfile("package/package.json"))
    entries.append({
        "name": name,
        "version": package_manifest["version"],
        "path": f"packages/{target_name}",
        "sha256": hashlib.sha256(data).hexdigest(),
    })
manifest["packages"] = entries
helpers = []
helper_dir = pathlib.Path(work_dir) / "helpers"
helper_dir.mkdir()
for helper_name in ("dsh-profile.py", "verify-dsh-dsv-assets.py", "dsh-credentials.py"):
    source = pathlib.Path(repo_root) / "scripts" / helper_name
    if not source.is_file():
        raise SystemExit(f"installer helper is missing: {source}")
    target = helper_dir / helper_name
    shutil.copy2(source, target)
    helpers.append({
        "name": helper_name,
        "path": f"helpers/{helper_name}",
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
    })
manifest["helpers"] = helpers
manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()
root = pathlib.Path(work_dir) / "archive-root"
root.mkdir()
(root / "manifest.json").write_bytes(manifest_bytes)
(root / "packages").mkdir()
shutil.copytree(helper_dir, root / "helpers")
for entry in entries:
    shutil.copy2(package_dir / pathlib.Path(entry["path"]).name, root / entry["path"])
archive_path = pathlib.Path(output_dir) / manifest["assetName"]
with tarfile.open(archive_path, "w:gz") as archive:
    archive.add(root / "manifest.json", arcname="manifest.json")
    for entry in entries:
        archive.add(root / entry["path"], arcname=entry["path"])
    for entry in helpers:
        archive.add(root / entry["path"], arcname=entry["path"])
(pathlib.Path(output_dir) / "manifest.json").write_bytes(manifest_bytes)
archive_sha = hashlib.sha256(archive_path.read_bytes()).hexdigest()
(pathlib.Path(output_dir) / f"{archive_path.name}.sha256").write_text(f"{archive_sha}  {archive_path.name}\n", encoding="utf-8")
subprocess.run([sys.executable, verifier, str(archive_path), str(pathlib.Path(output_dir) / "manifest.json")], check=True)
print(archive_path)
PY
