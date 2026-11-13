# credrot

**A fully offline, deterministic credential-lifecycle ledger for local batch collection tasks.**

`credrot` manages credential *metadata*: which data source a credential belongs to,
which local task depends on it, whether it is expiring, how a rotation batch is planned,
and which verified credential a task should use right now. It never stores a secret.

> **Compliance first.** Use `credrot` only for credentials and local tasks that you own or
> are explicitly authorized to operate. This repository contains synthetic placeholders
> shaped like `SYNTH-DS-ALPHA-reader-01`; there are no real credentials, real company names,
> real target domains, or external service calls. The verification stub binds only to
> `127.0.0.1`. See [docs/SECURITY.md](docs/SECURITY.md).

## What it is

- A local SQLite ledger for credential metadata, task bindings, rotation batches,
  verification observations, and audit events.
- A five-state credential machine: `active`, `standby`, `rotating`, `expired`, `revoked`.
- A deterministic planner driven by expiry horizons, task roles, minimum active coverage,
  backup margins, and task priority.
- A transactional, idempotent rotation executor that records every transition.
- A loopback verification stub plus a health-conclusion engine
  (`healthy / degraded / failing`).
- A task resolver that prefers verified active credentials, falls back to verified standby
  credentials, and returns a structured failure explanation when nothing is usable.
- Deterministic Markdown + JSON reports and batch archive snapshots.
- A small local Flask API and static home page. No CDN, no browser automation, no cloud SDK.

## What it is not

- Not a secret vault or KMS: no plaintext secrets, encryption, HSM, or secret delivery.
- Not a crawler or collection executor: it only hands a task a credential metadata choice.
- Not a dashboard product: the home page is a local read-only index.
- Not a scheduler, cluster coordinator, or multi-tenant approval system.

## Quick start (5 minutes)

Requirements: Python 3.10+, pip, and a local shell. Installation is the only step that may
touch the network.

```bash
python -m venv .venv
# Linux/macOS
. .venv/bin/activate
# Windows PowerShell
# .venv\Scripts\Activate.ps1

pip install -e ".[test]"
```

Seed the synthetic fixtures, run the tests, and start the local surface:

```bash
python -m credrot init-demo
pytest -q
python -m credrot serve
```

Open <http://127.0.0.1:8080> for the local credential table and recent batches.

The default ledger is `.credrot/credrot.db` and is ignored by git. No SQLite runtime file is
part of the deliverable.

### Full local lifecycle

Use a fixed clock for reproducible output. In one terminal:

```bash
python -m credrot verify-stub --port 8777
```

In another:

```bash
python -m credrot init-demo
python -m credrot plan --now 2025-01-01T00:00:00Z
# copy the batch_id printed above
python -m credrot rotate --batch BATCH-20250101T000000Z-XXXXXXXXXX --now 2025-01-01T00:00:00Z
python -m credrot verify --batch BATCH-20250101T000000Z-XXXXXXXXXX --now 2025-01-01T00:00:00Z
python -m credrot report --batch BATCH-20250101T000000Z-XXXXXXXXXX \
  --now 2025-01-01T00:00:00Z --out reports
python -m credrot archive --batch BATCH-20250101T000000Z-XXXXXXXXXX \
  --now 2025-01-01T00:00:00Z
```

Or run the scripted version from a POSIX shell (Git Bash on Windows):

```bash
scripts/run_demo.sh
```

`scripts/run_checks.sh` seeds data, compiles the package, and runs the complete test suite.

## Example commands

```bash
python -m credrot init-demo
python -m credrot plan --now 2025-01-01T00:00:00Z --format markdown
python -m credrot rotate --batch <batch-id> --now 2025-01-01T00:00:00Z
python -m credrot verify --batch <batch-id> --stub-url http://127.0.0.1:8777
python -m credrot resolve --task TASK-ALPHA-DAILY --now 2025-01-01T00:00:00Z
python -m credrot report --batch <batch-id> --format both --out reports
python -m credrot archive --batch <batch-id>
python -m credrot serve --port 8080
```

Useful HTTP endpoints, all bound to loopback by the server command:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/credentials` | Current metadata and latest health observation |
| GET | `/api/plan?now=...` | Build and persist a deterministic plan |
| GET | `/api/health` | Service, ledger, latest verification and batch summary |
| GET | `/api/report?batch_id=...&format=json\|markdown` | Deterministic report |
| POST | `/api/rotate` | Execute an already-generated `batch_id` only |

`POST /api/rotate` accepts `{"batch_id": "..."}` and nothing that directly changes a
credential state. Unknown plans return `404`; a failed transaction returns `409`.

## What the demo fixtures exercise

- Ten synthetic credentials across three synthetic sources (`DS-ALPHA`, `DS-BETA`,
  `DS-GAMMA`).
- Three roles: reader, writer, admin.
- A critical expiry, an expiring-soon credential, a redundancy promotion, a revoked
  credential, a multi-role task, and a task whose only candidate is deliberately invalid in
  the stub rules.
- Stub rules produce `valid`, `invalid`, and `unknown` verdicts without any external call.

## Determinism

`build_plan` is a pure function of credentials, tasks, policy, and an injected clock. Items
are ordered by urgency, then task priority, then credential ID. Batch IDs are derived from
the fixed timestamp and normalized plan content. Reports use fixed clock values, sorted
keys, and no process IDs or wall-clock random values, so two reports rendered from the same
ledger state and fixed clock are byte-identical.

Rotation is transactional: any failure rolls back all credential changes and persists the
failure reason on the batch and failed item. Re-running a completed batch returns all
`skipped`; retrying a failed batch is allowed after the cause is corrected.

## Tests

```bash
pytest -q
```

The suite contains **62 collected tests** covering configuration, schema/transactions,
registry validation, state transitions, planning, rotation/idempotency/rollback, stub
verification (valid, invalid, unknown, timeout, connection refused), resolution and
fallbacks, audit, reports, HTTP handlers, and an end-to-end two-database determinism check.
The project targets **≥80% line coverage** (coverage is a stated target, not a pytest
dependency). The delivery is within the 2,500–4,500 line Python budget including tests, and
every Python file is ≤300 lines.

## Directory map

```text
credrot/
├─ pyproject.toml
├─ README.md
├─ docs/                      DESIGN, OPS, SECURITY
├─ credrot/
│  ├─ __main__.py             CLI: init-demo, plan, rotate, verify, report, archive, serve
│  ├─ app.py / api.py         Flask assembly and thin HTTP layer
│  ├─ clock.py                SystemClock, FixedClock, UTC helpers
│  ├─ config.py               Policy loading/validation
│  ├─ db.py / store.py        SQLite schema, transactions, repositories
│  ├─ verification_store.py   Verification persistence
│  ├─ model.py                Credential, Task, PlanItem, RotationPlan, AuditEvent
│  ├─ state_machine.py        Transition whitelist and clock-driven expiry
│  ├─ registry.py             Fixture validation (accumulates errors)
│  ├─ planner.py              Pure deterministic plan builder
│  ├─ rotator.py              Transactional/idempotent execution
│  ├─ verifier.py             Loopback-only client and health conclusion
│  ├─ resolver.py             Task → verified credential resolution
│  ├─ audit.py                Append-only audit trail
│  ├─ report.py               Report payload + JSON
│  ├─ report_markdown.py      Markdown renderer
│  ├─ plan_export.py          Plan Markdown export
│  ├─ archive.py              Batch archive snapshot
│  ├─ stubs/verify_stub.py    Loopback verification stub
│  └─ web/                    Local template + CSS
├─ fixtures/                  Synthetic YAML data and stub rules
├─ scripts/                   run_demo.sh, run_checks.sh
└─ tests/                     62 tests across 12 modules + conftest
```

## Documentation

- [docs/DESIGN.md](docs/DESIGN.md) — state machine, plan algorithm, ordering, audit,
  idempotency, transaction boundaries.
- [docs/OPS.md](docs/OPS.md) — add credentials, review plans, rotate, roll back, troubleshoot.
- [docs/SECURITY.md](docs/SECURITY.md) — authorization boundary, data-handling rules,
  loopback-only stub, limitations.

## Explicit non-goals

Real secret encryption/storage, KMS/HSM integration, credential issuance, real data-source
adapters, collection execution, visual plan editing, multi-user roles/approvals, scheduling,
cluster coordination, distributed locks, cross-host credential diffusion audit, SCIM/identity
sync, front-end frameworks, and chart dashboards are intentionally out of scope.
