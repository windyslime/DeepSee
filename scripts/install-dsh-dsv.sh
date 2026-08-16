#!/usr/bin/env bash
set -euo pipefail

readonly DEFAULT_VERSION="0.1.0"
readonly RELEASE_TAG_PREFIX="dsh-dsv-v"
readonly RELEASE_BASE="https://github.com/windyslime/DeepSee/releases/download"
readonly RAW_BASE="https://raw.githubusercontent.com/windyslime/DeepSee/main/scripts"
readonly API_KEY_REF="DEEPSEE_DSV_API_KEY"

action=install
dry_run=0
configure_mode=""
force_rotate=0
version="${DEEPSEE_DSV_VERSION:-$DEFAULT_VERSION}"
dsh_home="${DSH_HOME:-$HOME/.dsh}"
gateway_url="${DEEPSEE_GATEWAY_URL:-http://127.0.0.1:8712}"
installer_base="${DEEPSEE_INSTALLER_BASE_URL:-$RAW_BASE}"
release_base="${DEEPSEE_RELEASE_BASE_URL:-$RELEASE_BASE}"
tty_open=0

usage() {
  cat <<'EOF'
DeepSee DSV installer for DSH Web profiles.

Usage:
  install-dsh-dsv.sh [--configure|--no-configure] [--dry-run] [--version VERSION]
  install-dsh-dsv.sh --verify [--version VERSION]
  install-dsh-dsv.sh --uninstall [--version VERSION]

Configuration choices:
  --configure     configure the DeepSee gateway and DSV public key
  --no-configure  install packages and preserve existing credentials/configuration
  --verify        read-only profile and gateway check
  --uninstall     remove the managed DSV layer but keep credentials

Environment:
  DSH_HOME                    DSH home directory (default: ~/.dsh)
  DEEPSEE_DSV_VERSION         Release version without the dsh-dsv-v prefix
  DEEPSEE_GATEWAY_URL         DeepSee gateway URL (default: http://127.0.0.1:8712)
  DEEPSEE_DSV_API_KEY         DSV public key used by --configure (never printed)
  DEEPSEE_INSTALLER_BASE_URL  Override raw script base for local testing
EOF
}

set_configuration_mode() {
  local requested=$1
  if [[ -n "$configure_mode" && "$configure_mode" != "$requested" ]]; then
    printf '%s\n' '--configure and --no-configure are mutually exclusive' >&2
    exit 2
  fi
  configure_mode=$requested
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --configure)
      set_configuration_mode configure
      force_rotate=1
      shift
      ;;
    --no-configure)
      set_configuration_mode no-configure
      shift
      ;;
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
if [[ "$action" != install && -n "$configure_mode" ]]; then
  printf '%s\n' '--configure/--no-configure can only be used with installation' >&2
  exit 2
fi

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) printf 'unsupported platform: %s\n' "$(uname -s)" >&2; exit 1 ;;
esac

for command_name in curl python3 node tar; do
  command -v "$command_name" >/dev/null 2>&1 || {
    printf 'required command is missing: %s\n' "$command_name" >&2
    exit 1
  }
done
if [[ "$action" != verify ]]; then
  command -v pnpm >/dev/null 2>&1 || {
    printf 'required command is missing: pnpm\n' >&2
    exit 1
  }
fi

profile="$dsh_home/profiles/web"
cache="$dsh_home/cache/deepsee-dsv/$version"
asset="$cache/deepsee-dsh-dsv-v$version.tar.gz"
profile_tool="$cache/dsh-profile.py"
verifier="$cache/verify-dsh-dsv-assets.py"
credential_tool="$cache/dsh-credentials.py"
credentials="$dsh_home/.credentials.yaml"
archive_url="$release_base/$RELEASE_TAG_PREFIX$version/deepsee-dsh-dsv-v$version.tar.gz"

if [[ ! -d "$profile" || ! -f "$profile/package.json" ]]; then
  printf 'DSH Web profile not found: %s\n' "$profile" >&2
  printf 'Set DSH_HOME to the directory containing profiles/web.\n' >&2
  exit 1
fi

choose_configuration() {
  if [[ "$action" != install || -n "$configure_mode" ]]; then
    return
  fi
  if [[ "$dry_run" == 1 ]]; then
    configure_mode=no-configure
    return
  fi
  if ! open_tty; then
    printf '%s\n' 'non-interactive installation requires --configure or --no-configure' >&2
    exit 2
  fi
  printf '%s\n' 'Configure DeepSee connection automatically? [Y/n/c]' >&3
  printf '%s\n' 'Y  configure gateway URL and ask for the DSV public key when needed' >&3
  printf '%s\n' 'n  install the DSV packages only; keep existing credentials and config' >&3
  printf '%s\n' 'c  cancel before changing the profile' >&3
  local answer
  while true; do
    if ! IFS= read -r answer <&3; then
      printf '%s\n' 'installation cancelled: no selection was received' >&2
      exit 2
    fi
    case "${answer:-Y}" in
      Y|y) configure_mode=configure; return ;;
      N|n) configure_mode=no-configure; return ;;
      C|c) printf '%s\n' 'installation cancelled; no files were changed' >&3; exit 0 ;;
      *) printf '%s\n' 'Please enter Y, n, or c.' >&3 ;;
    esac
  done
}

open_tty() {
  if [[ "$tty_open" == 1 ]]; then
    return 0
  fi
  if ! exec 3<>/dev/tty 2>/dev/null; then
    return 1
  fi
  tty_open=1
}

choose_configuration

if [[ "$dry_run" == 1 ]]; then
  printf 'dry-run: would install DSV %s into %s\n' "$version" "$profile"
  printf 'dry-run: configuration mode: %s\n' "$configure_mode"
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
if [[ "$configure_mode" == configure ]]; then
  [[ -f "$credential_tool" ]] || download "$installer_base/dsh-credentials.py" "$credential_tool"
  chmod +x "$credential_tool"
fi
chmod +x "$profile_tool" "$verifier"
[[ -f "$asset" ]] || download "$archive_url" "$asset"
python3 "$verifier" "$asset"

if [[ ! -f "$cache/manifest.json" || ! -d "$cache/packages" ]]; then
  tar -xzf "$asset" -C "$cache"
fi
python3 "$verifier" "$asset" "$cache/manifest.json"

run_profile_action() {
  local profile_action=$1
  local dry_flag=${2:-}
  local command=(python3 "$profile_tool" "$profile_action" --profile "$profile" --asset-root "$cache")
  if [[ -n "$dry_flag" ]]; then
    command+=("$dry_flag")
  fi
  if [[ "$configure_mode" == configure && "$profile_action" == install ]]; then
    command+=(--gateway-url "$gateway_url" --api-key-ref "$API_KEY_REF")
  fi
  "${command[@]}"
}

if [[ "$action" == verify ]]; then
  run_profile_action verify
  if curl --fail --silent --show-error --max-time 3 "${gateway_url%/}/health" >/dev/null; then
    printf 'DeepSee gateway reachable: %s\n' "${gateway_url%/}"
  else
    printf 'DSH DSV profile verified; gateway not reachable at %s\n' "${gateway_url%/}"
    printf 'Start it with: deepsee-server\n'
  fi
  exit 0
fi

credential_update=0
credential_value=""
if [[ "$configure_mode" == configure ]]; then
  if [[ "$force_rotate" == 0 ]] && python3 "$credential_tool" has --file "$credentials" --name "$API_KEY_REF" >/dev/null 2>&1; then
    printf 'Existing %s retained; use --configure with DEEPSEE_DSV_API_KEY to rotate it.\n' "$API_KEY_REF"
  else
    credential_update=1
    if [[ -n "${DEEPSEE_DSV_API_KEY+x}" && -n "$DEEPSEE_DSV_API_KEY" ]]; then
      credential_value=$DEEPSEE_DSV_API_KEY
    else
      if ! open_tty; then
        printf '%s\n' '--configure requires DEEPSEE_DSV_API_KEY or an interactive TTY prompt' >&2
        exit 2
      fi
      printf 'DSV public key (input hidden): ' >&3
      if ! IFS= read -r -s credential_value <&3; then
        printf '\n%s\n' 'could not read the DSV public key' >&2
        exit 2
      fi
      printf '\n' >&3
    fi
    if [[ -z "$credential_value" ]]; then
      printf '%s\n' 'DSV public key cannot be empty' >&2
      exit 2
    fi
  fi
  if ! run_profile_action install --dry-run >/dev/null; then
    printf '%s\n' 'gateway URL or profile configuration is invalid; no files were changed' >&2
    exit 2
  fi
fi

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
if [[ "$configure_mode" == configure ]]; then
  if [[ -e "$credentials" ]]; then
    cp -p "$credentials" "$backup/.credentials.yaml"
  else
    : > "$backup/.credentials.yaml.missing"
  fi
fi

restore_backup() {
  for profile_file in package.json cordis.patch.yml pnpm-lock.yaml; do
    if [[ -e "$backup/$profile_file" ]]; then
      cp -p "$backup/$profile_file" "$profile/$profile_file"
    elif [[ -e "$backup/$profile_file.missing" ]]; then
      rm -f "$profile/$profile_file"
    fi
  done
  if [[ "$configure_mode" == configure ]]; then
    if [[ -e "$backup/.credentials.yaml" ]]; then
      cp -p "$backup/.credentials.yaml" "$credentials"
    elif [[ -e "$backup/.credentials.yaml.missing" ]]; then
      rm -f "$credentials"
    fi
  fi
}

if [[ "$configure_mode" == configure && "$credential_update" == 1 ]]; then
  if ! printf '%s\n' "$credential_value" | python3 "$credential_tool" set --file "$credentials" --name "$API_KEY_REF" --stdin >/dev/null; then
    printf 'credential update failed; restoring profile backup: %s\n' "$backup" >&2
    restore_backup
    exit 1
  fi
  unset credential_value
fi

if ! run_profile_action "$action"; then
  printf 'profile update failed; restoring backup: %s\n' "$backup" >&2
  restore_backup
  exit 1
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

if curl --fail --silent --show-error --max-time 3 "${gateway_url%/}/health" >/dev/null; then
  printf 'DeepSee gateway reachable: %s\n' "${gateway_url%/}"
else
  printf 'DSH DSV installed for version %s; gateway not reachable at %s\n' "$version" "${gateway_url%/}"
  printf 'Start it with: deepsee-server\n'
fi

if [[ "$action" == uninstall ]]; then
  printf 'DSH DSV layer removed. Backup retained at: %s\n' "$backup"
else
  printf 'DSH DSV installed (%s). Backup retained at: %s\n' "$configure_mode" "$backup"
fi
