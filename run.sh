#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
source scripts/operations.sh
case "${1:-}" in
  build|deploy|start|start-shards|export|cost)
    action="$1"; shift
    if [[ "$action" == start-shards ]]; then
      docintel_start_shards "$@"
    else
      "docintel_${action}" "$@"
    fi ;;
  -h|--help|"") docintel_usage ;;
  *) docintel_usage >&2; exit 2 ;;
esac
