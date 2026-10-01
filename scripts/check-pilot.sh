#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
  tests/test_pilot_sources.py tests/test_pilot.py tests/test_pilot_api.py tests/test_pilot_server.py \
  tests/test_docintel.py tests/test_enrichment.py tests/test_config.py -q
bash -n scripts/pilot-dev.sh
cd frontend
./node_modules/.bin/eslint .
./node_modules/.bin/tsc --noEmit --incremental false
node --test tests/*.test.mjs