#!/usr/bin/env bash
# Full verification: Python unit + integration tests, then the Node frontend
# smoke test. Stdlib-only backend; no pip installs required.
set -euo pipefail
cd "$(dirname "$0")"

echo "== Python tests (40 cases) =="
python3 -I -m unittest discover -s tests -v

echo
echo "== Frontend render smoke test =="
node tests/smoke_frontend.js

echo
echo "All checks passed."
