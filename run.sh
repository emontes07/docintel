#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
source scripts/operations.sh
case "${1:-}" in
  build|deploy|start|export|cost)
    action="$1"; shift; "docintel_${action}" "$@" ;;
  -h|--help|"") docintel_usage ;;
  *) docintel_usage >&2; exit 2 ;;
esac
