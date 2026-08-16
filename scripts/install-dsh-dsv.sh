#!/usr/bin/env bash
set -euo pipefail

readonly DEFAULT_VERSION="0.1.0"
readonly RELEASE_TAG_PREFIX="dsh-dsv-v"
readonly RELEASE_BASE="https://github.com/windyslime/DeepSee/releases/download"
readonly RAW_BASE="https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts"

action=install
dry_run=0
version="${DEEPSEE_DSV_VERSION:-$DEFAULT_VERSION}"
dsh_home="${DSH_HOME:-$HOME/.dsh}"
gateway_url="${DEEPSEE_GATEWAY_URL:-http://127.0.0.1:8712}"
installer_base="${DEEPSEE_INSTALLER_BASE_URL:-$RAW_BASE}"
release_base="${DEEPSEE_RELEASE_BASE_URL:-$RELEASE_BASE}"

usage() {
  cat <<'EOF'
DeepSee DSV installer for DSH Web profiles.

Usage:
  install-dsh-dsv.sh [--dry-run] [--version VERSION]
  install-dsh-dsv.sh --verify [--version VERSION]
  install-dsh-dsv.sh --uninstall [--version VERSION]

Environment:
  DSH_HOME                    DSH home directory (default: ~/.dsh)
  DEEPSEE_DSV_VERSION         Release version without the dsh-dsv-v prefix
  DEEPSEE_GATEWAY_URL         DeepSee gateway URL (default: http://127.0.0.1:8712)
  DEEPSEE_INSTALLER_BASE_URL  Override raw script base for local testing
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) dry_run=1; shift ;;
    --verify) action=verify; shift ;;
    --uninstall) action=uninstall; shift ;;
    --version)
      [[ $# -ge 2 ]] || { printf '%s\n' '--version requires a value' >&2; exit 2; }
      version=$2
      shift 2
      ;;
    --help|-h) usage; exit 0 ;;
    *) printf 'unknown option: %s\n' "$1" >&2; usage; exit 2 ;;
  esac
done

if [[ "$dry_run" == 1 && "$action" != install ]]; then
  printf '%s\n' '--dry-run can only be used with installation' >&2
  exit 2
fi

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) printf 'unsupported platform: %s\n' "$(uname -s)" >&2; exit 1 ;;
esac

for command_name in curl python3 node pnpm; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'required command is missing: %s\n' "$command_name" >&2
    exit 1
  }
done

profile="$dsh_home/profiles/web"
cache="$dsh_home/cache/deepsee-dsv/$version"
asset="$cache/deepsee-dsh-dsv-v$version.tar.gz"
profile_tool="$cache/dsh-profile.py"
verifier="$cache/verify-dsh-dsv-assets.py"
archive_url="$release_base/$RELEASE_TAG_PREFIX$version/deepsee-dsh-dsv-v$version.tar.gz"

if [[ ! -d "$profile" || ! -f "$profile/package.json" ]]; then
  printf 'DSH Web profile not found: %s\n' "$profile" >&2
  printf 'Set DSH_HOME to the directory containing profiles/web.\n' >&2
  exit 1
fi

if [[ "$dry_run" == 1 ]]; then
  printf 'dry-run: would install DSV %s into %s\n' "$version" "$profile"
  printf 'dry-run: would download %s\n' "$archive_url"
  printf 'dry-run: no files were changed\n'
  exit 0
fi

mkdir -p "$cache"
download() {
  local url=$1
  local destination=$2
  local temporary="${destination}.download"
  curl --fail --location --silent --show-error --retry 2 --output "$temporary" "$url"
  mv "$temporary" "$destination"
}

[[ -f "$profile_tool" ]] || download "$installer_base/dsh-profile.py" "$profile_tool"
[[ -f "$verifier" ]] || download "$installer_base/verify-dsh-dsv-assets.py" "$verifier"
chmod +x "$profile_tool" "$verifier"
[[ -f "$asset" ]] || download "$archive_url" "$asset"
python3 "$verifier" "$asset"

if [[ ! -f "$cache/manifest.json" || ! -d "$cache/packages" ]]; then
  tar -xzf "$asset" -C "$cache"
fi
python3 "$verifier" "$asset" "$cache/manifest.json"

backup_root="$profile/.deepsee-dsv-backups"
backup="$backup_root/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup"
for profile_file in package.json cordis.patch.yml pnpm-lock.yaml; do
  if [[ -e "$profile/$profile_file" ]]; then
    cp -p "$profile/$profile_file" "$backup/$profile_file"
  else
    : > "$backup/$profile_file.missing"
  fi
done

restore_backup() {
  for profile_file in package.json cordis.patch.yml pnpm-lock.yaml; do
    if [[ -e "$backup/$profile_file" ]]; then
      cp -p "$backup/$profile_file" "$profile/$profile_file"
    elif [[ -e "$backup/$profile_file.missing" ]]; then
      rm -f "$profile/$profile_file"
    fi
  done
}

run_profile_action() {
  python3 "$profile_tool" "$1" --profile "$profile" --asset-root "$cache"
}

if [[ "$action" == uninstall ]]; then
  run_profile_action uninstall
else
  run_profile_action install
fi

if ! pnpm --dir "$profile" install --lockfile-only=false; then
  printf 'pnpm install failed; restoring profile backup: %s\n' "$backup" >&2
  restore_backup
  exit 1
fi

if command -v dsh >/dev/null 2>&1; then
  if ! dsh --profile web --dump-config >/dev/null; then
    printf 'DSH Loader validation failed; restoring profile backup: %s\n' "$backup" >&2
    restore_backup
    exit 1
  fi
else
  printf 'warning: dsh command not found; run `dsh --profile web --dump-config` to validate Loader composition\n' >&2
fi

if [[ "$action" == verify ]]; then
  run_profile_action verify
fi

if curl --fail --silent --show-error --max-time 3 "${gateway_url%/}/health" >/dev/null; then
  printf 'DeepSee gateway reachable: %s\n' "${gateway_url%/}"
else
  printf 'DSH DSV installed for version %s; gateway not reachable at %s\n' "$version" "${gateway_url%/}"
  printf 'Start it with: deepsee-server\n'
fi

if [[ "$action" == uninstall ]]; then
  printf 'DSH DSV layer removed. Backup retained at: %s\n' "$backup"
else
  printf 'DSH DSV installed. Backup retained at: %s\n' "$backup"
fi
