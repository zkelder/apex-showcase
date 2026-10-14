#!/usr/bin/env bash
# Regenerate the static demo's data from Apex's own fixture services.
#
#   1. deterministic outcomes: runs the eight offline fixtures (no database, no network)
#   2. retained-console snapshot: seeds a THROWAWAY Postgres (its own Compose project and
#      port, never the default one), runs the fixture-only governed PAPER demo, starts the
#      operator API on loopback, crawls it with GET requests, and tears everything down.
#
# Needs: uv, Docker (for step 2 only), Node is not required.
# Usage: scripts/demo/regenerate_demo_data.sh [--outcomes-only]
set -euo pipefail
cd "$(dirname "$0")/../.."

uv run python scripts/demo/generate_demo_outcomes.py
[ "${1:-}" = "--outcomes-only" ] && exit 0

export COMPOSE_PROJECT_NAME=apex-demo-seed
export APEX_POSTGRES_PORT=43200
export APEX_POSTGRES_HOST=127.0.0.1
export APEX_POSTGRES_TEST_DB=apex_demo_seed_test
API_PORT=8765
API_PID=""

cleanup() {
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null || true
  uv run python scripts/dev.py db-down --volumes >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Refuse to continue if the isolated identity is not what we expect.
uv run python scripts/dev.py db-identity | tee /dev/stderr | grep -q "apex-demo-seed"
uv run python scripts/dev.py db-down --volumes
uv run python scripts/dev.py db-up
uv run python scripts/dev.py migrate
uv run python -m apex.cli evidence-review-paper-trial-demo >/dev/null

uv run python -m uvicorn apex.operator_api:app --host 127.0.0.1 --port "$API_PORT" >/dev/null 2>&1 &
API_PID=$!
for _ in $(seq 1 60); do
  curl -fs "http://127.0.0.1:${API_PORT}/api/v1/health" >/dev/null 2>&1 && break
  sleep 1
done
uv run python scripts/demo/generate_demo_snapshot.py --base "http://127.0.0.1:${API_PORT}"
