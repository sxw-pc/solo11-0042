# credrot Operations

This is a local, single-operator runbook. Every example is synthetic and loopback-only.

## 1. Daily loop

```bash
# 1. Refresh a known demo ledger (destructive; see backup below)
python -m credrot init-demo

# 2. Inspect the deterministic plan at a fixed instant
python -m credrot plan --now 2025-01-01T00:00:00Z

# 3. Start the local verification stub in its own terminal
python -m credrot verify-stub --port 8777

# 4. Execute only the reviewed batch
python -m credrot rotate --batch <batch-id> --now 2025-01-01T00:00:00Z

# 5. Observe health and task resolvability
python -m credrot verify --batch <batch-id> --now 2025-01-01T00:00:00Z
python -m credrot resolve --task TASK-ALPHA-DAILY --now 2025-01-01T00:00:00Z

# 6. Produce durable evidence
python -m credrot report --batch <batch-id> --now 2025-01-01T00:00:00Z --out reports
python -m credrot archive --batch <batch-id> --now 2025-01-01T00:00:00Z
```

`plan` both prints and persists the batch. `rotate` accepts only a batch ID that exists in
the ledger; it cannot be asked to jump a credential to an arbitrary state. `report` writes
`report.json` and `report.md` under the output directory. No step other than dependency
installation needs internet access.

## 2. Add or change credential metadata

1. Edit `fixtures/credentials.yaml`:
   - choose an uppercase `CRED-*` ID and `DS-*` source;
   - role must be `reader`, `writer`, or `admin`;
   - keep `synthetic_ref` exactly `SYNTH-<source>-<role>-<two-digit seq>`;
   - compute `fingerprint = "sha256:" + sha256(synthetic_ref).hexdigest()`;
   - use quoted UTC timestamps and a status from the five-state set.
2. Add the ID to the relevant task in `fixtures/tasks.yaml`. The task source, required role,
   and credential source/role must agree. Binding order is fallback order.
3. Add a stub rule for the new fingerprint in `fixtures/verify_stub_rules.yaml` if its
   health must be simulated. Unknown fingerprints deliberately return `unknown`.
4. Validate before touching the operational ledger:

```bash
python -c "from pathlib import Path; from credrot.registry import load_demo_registry; r=load_demo_registry(Path('fixtures')); print([i.render() for i in r.issues])"
```

An empty list means the fixture set is structurally valid. The loader accumulates all errors
instead of stopping at the first one.

5. Seed a dedicated ledger with `--db`:

```bash
python -m credrot init-demo --db .credrot/staging.db --fixtures fixtures
```

**Warning:** `init-demo` clears credentials, tasks, batches, verification observations, and
audits in the selected database. It is a reset command, not a merge command.

## 3. Reviewing a plan

Check these fields before execution:

- `batch_id`: copy it exactly; the same fixed clock and unchanged ledger reproduce it.
- `reason_code`: `EXPIRED`, `CRITICAL_EXPIRY`, `EXPIRING_SOON`, or `REDUNDANCY_LOW`.
- `action`: `rotate` renews metadata and bumps `version`; `promote` makes a standby active
  without a version bump.
- `target_state`: `active` keeps the credential primary; `standby` keeps it as backup.
- `affected_tasks`: tasks that bind the credential and require its role.
- `summary.backup_deficits`: a non-empty map means the task has fewer standby candidates than
  its `backup_margin`. The planner cannot invent credentials; resolve the staffing gap in
  fixtures or in a later iteration.
- `summary.truncated`: nonzero means `max_items_per_plan` cut lower-urgency items. Raise the
  cap deliberately or generate another plan after the first batch.
- `precondition`: the per-item snapshot (`version`, `status`, `effective_state`,
  `expires_at`) captured at planning time. Execution rejects the batch if the live row no
  longer matches it; do not hand-edit this block.

For a dry export, use `plan --format markdown` or read `/api/plan?now=...`.

## 4. Executing and re-running

```bash
python -m credrot rotate --batch <batch-id>
python -m credrot rotate --batch <batch-id>   # safe: all skipped
```

- Success means status `completed` and every item `ok: true`.
- A completed batch is immutable through this CLI: repeated calls return `skipped` and do
  not touch credential rows.
- If any item fails, the whole credential transaction rolls back. The batch becomes `failed`,
  the first failure reason is retained, and one failed rotation item is recorded. The
  structured `failure_code` distinguishes the two snapshot-contract failures:
  - `SUPERSEDED_BY_BATCH`: another batch changed the credential's version, stored state, or
    validity deadline after this plan was generated. Generate a fresh plan instead of
    forcing the stale one.
  - `SNAPSHOT_STALE`: no other batch wrote the row; the credential simply passed its
    snapshot `expires_at` before execution. Re-plan at the current clock so the renewal is
    planned against the expired effective state.
  Other failures (for example, a revoked credential) keep their text reason with a null
  code. Fix the cause, then invoke rotate again; a failed batch is safe to re-run because
  every attempt rolls back and leaves credential versions and states untouched, recording
  the same single failure row until the cause is resolved.

## 5. Backup and rollback

Rotation is forward-only. There is no automatic "unrotate" command because audit history and
task consumers may already have observed the new metadata.

Before a planned operational batch:

```bash
cp .credrot/credrot.db .credrot/backup-before-<batch-id>.db
# if a -wal file exists, stop the service first so the backup is complete
```

To roll back a completed batch:

1. Stop `credrot serve` and any writer.
2. Preserve evidence: run `archive --batch <id>` and copy `reports/`.
3. Restore the pre-batch database file (and remove stale `-wal`/`-shm` companions).
4. Restart the service and run `GET /api/credentials` and `resolve` to confirm the intended
   state.

A failed transaction needs no manual rollback; the rotator already rolled it back. Use
`archive` to keep a batch-scoped record before any manual database restore.

## 6. Troubleshooting

### Stub connection refused or timeout

Symptoms: `verify` results have `STUB_UNREACHABLE` or `TIMEOUT`; resolution fails with
`NO_HEALTHY_CANDIDATE` or `UNVERIFIED`.

- Confirm the stub process is running: `python -m credrot verify-stub --port 8777`.
- Confirm the URL is loopback: `http://127.0.0.1:8777`. The verifier rejects every other host
  before making a request. Do not configure a proxy or external URL.
- Confirm `policy.stub.port` matches the stub process. The CLI default is `8777`.
- Timeout rules are deliberate in tests; production fixture rules have `delay_ms: 0`.
- Re-run `verify`; observations are append-only and the resolver uses the latest row.

### The plan is empty

- Check `plan --now ...`: all credentials may be outside the horizon.
- Confirm statuses are not `revoked`; revoked credentials never plan.
- Check `rotation.horizon_days` and `critical_horizon_days` in `fixtures/policy.yaml`.
- An empty plan is valid and completes as a no-op batch.

### A task cannot be resolved

Inspect `/api/credentials` and the resolver's `attempts` array:

- `UNVERIFIED`: no fresh observation exists. Run `verify`.
- `STALE_VERIFICATION`: the latest observation is older than
  `resolver.max_verification_age_minutes`. Re-run `verify` with the current clock.
- `STATE_EXPIRED` / `STATE_REVOKED` / `STATE_ROTATING`: the candidate is not usable. Review
  the plan, complete the rotation, or update fixture metadata.
- `VERIFICATION_INVALID`, `VERIFICATION_UNKNOWN`, `VERIFICATION_ERROR`: inspect the stub rule
  for that fingerprint and the stub logs.
- `NO_MATCHING_ROLE` or `NO_BINDINGS`: fix the task-to-credential binding or required roles.

The demo's `TASK-GAMMA-AUDIT` is intentionally unresolvable: its only admin credential has an
`invalid` stub rule. That gives the report a realistic failure row.

### Invalid fixtures

Run the validation snippet in section 2. Common issue codes:

- `FINGERPRINT_MISMATCH` — recompute `sha256(synthetic_ref)`.
- `SYNTHETIC_REF_MISMATCH` — match `SYNTH-<source>-<role>-<seq>` exactly.
- `UNKNOWN_BINDING` — task references a credential ID that is not loaded.
- `BINDING_SOURCE` / `BINDING_ROLE` — task and credential disagree.
- `POLICY_RANGE` / `STUB_HOST` — fix policy values; the stub host must be `127.0.0.1`.

### Database is locked

SQLite uses WAL and short write transactions. Stop duplicate writers and retry. Do not delete
`-wal` or `-shm` files while a process is open. For a clean backup, stop writers first.

### Report batch not found

`report --batch` requires an existing batch. List recent batches via the home page,
`/api/health`, or the SQLite `rotation_batches` table. Omit `--batch` to select the latest
batch deterministically by `generated_at DESC, batch_id DESC`.
