#!/usr/bin/env bash
set -euo pipefail
exec "$(cd "$(dirname "$0")" && pwd)/install-dsh-dsv.sh" --uninstall "$@"
