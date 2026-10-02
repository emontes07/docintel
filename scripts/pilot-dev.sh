#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
umask 077
export PYTHONDONTWRITEBYTECODE=1
if [[ ! -x .venv/bin/python ]]; then
  printf 'Existing .venv is missing. No installation attempted; see the clean-install gate in PILOT.md.\n' >&2
  exit 2
fi
exec .venv/bin/python -m backend.pilot_server