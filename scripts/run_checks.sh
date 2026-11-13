#!/usr/bin/env bash
# Offline verification gate: compile, seed, test, and enforce file/line budgets.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python -m compileall -q credrot tests
python -m credrot init-demo --db .credrot/checks.db --now 2025-01-01T00:00:00Z
python -m pytest -q

python - <<'PY'
from pathlib import Path

files = sorted(Path("credrot").rglob("*.py")) + sorted(Path("tests").rglob("*.py"))
total = 0
violations = []
for path in files:
    count = len(path.read_text(encoding="utf-8").splitlines())
    total += count
    if count > 300:
        violations.append(f"{path}: {count} > 300")
if violations:
    raise SystemExit("file-size violations:\n" + "\n".join(violations))
if not 2500 <= total <= 4500:
    raise SystemExit(f"Python line budget violated: {total} (expected 2500..4500)")
print(f"line budget: {total} Python lines across {len(files)} files; all files <= 300")
PY
