#!/usr/bin/env bash
# Offline demo: seed -> plan -> rotate -> verify -> report. No external requests.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

NOW="${CREDROT_DEMO_NOW:-2025-01-01T00:00:00Z}"
PORT="${CREDROT_STUB_PORT:-8777}"
DB=".credrot/demo.db"
mkdir -p .credrot reports/demo

python -m credrot verify-stub --port "$PORT" > .credrot/stub.log 2>&1 &
STUB_PID=$!

cleanup() {
  kill "$STUB_PID" 2>/dev/null || true
  wait "$STUB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# Wait briefly for the loopback socket to accept connections.
python - "$PORT" <<'PY'
import socket, sys, time
port = int(sys.argv[1])
for _ in range(50):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.1):
            break
    except OSError:
        time.sleep(0.1)
else:
    raise SystemExit(f"verify stub did not start on 127.0.0.1:{port}")
PY

echo "[1/6] Seeding synthetic fixtures into $DB"
python -m credrot init-demo --db "$DB" --now "$NOW"

echo "[2/6] Building deterministic plan"
PLAN_JSON="$(python -m credrot plan --db "$DB" --now "$NOW")"
BATCH="$(printf '%s' "$PLAN_JSON" | python -c 'import json,sys; print(json.load(sys.stdin)["batch_id"])')"
printf '%s\n' "$PLAN_JSON" > reports/demo/plan.json
echo "      batch: $BATCH"

echo "[3/6] Executing rotation transaction"
python -m credrot rotate --db "$DB" --batch "$BATCH" --now "$NOW" \
  > reports/demo/rotation.json

echo "[4/6] Verifying against 127.0.0.1:$PORT"
python -m credrot verify --db "$DB" --batch "$BATCH" --now "$NOW" \
  --stub-url "http://127.0.0.1:$PORT" > reports/demo/verification.json

echo "[5/6] Resolving tasks"
python -m credrot resolve --db "$DB" --task TASK-ALPHA-DAILY --now "$NOW" \
  > reports/demo/resolve-alpha.json
python -m credrot resolve --db "$DB" --task TASK-GAMMA-AUDIT --now "$NOW" \
  > reports/demo/resolve-gamma-failure.json || true

echo "[6/6] Rendering report and archive"
python -m credrot report --db "$DB" --batch "$BATCH" --now "$NOW" \
  --out reports/demo > reports/demo/report-manifest.json
python -m credrot archive --db "$DB" --batch "$BATCH" --now "$NOW" \
  --out reports/demo/archive.json > reports/demo/archive-manifest.json

cat <<EOF

Demo complete. Evidence is under reports/demo/.
Start the local page with:
  python -m credrot serve --db "$DB" --port 8080
Then open http://127.0.0.1:8080
EOF
