# credrot Design

This document describes the credential model, state machine, planning algorithm, audit
trail, and transaction semantics. All examples are synthetic.

## 1. Domain model

A **Credential** is metadata only:

| Field | Meaning |
| --- | --- |
| `id` | Stable synthetic identifier, e.g. `CRED-ALPHA-READER-01` |
| `source` | Synthetic data-source identifier, e.g. `DS-ALPHA` |
| `role` | `reader`, `writer`, or `admin` |
| `version` | Monotonic simulated rotation version |
| `synthetic_ref` | Visible placeholder `SYNTH-<source>-<role>-<seq>` |
| `fingerprint` | `sha256(synthetic_ref)` with a `sha256:` prefix |
| `issued_at`, `expires_at` | UTC ISO-8601 timestamps |
| `status` | Stored state; effective state may be `expired` due to the clock |

A **Task** is a local collection job with a source, priority (`1` is most important), one
or more required roles, `min_active`, `backup_margin`, and an ordered fallback list of
credential IDs. Binding order is the declared preference.

A **RotationPlan** has a deterministic `batch_id`, injected `generated_at`, ordered items,
and a summary. Each item carries a credential, action (`rotate` or `promote`), effective
`from_state`, `target_state`, reason code, human reason, days-to-expiry, affected tasks, and
minimum affected task priority. The stored plan is the authorization for `POST /api/rotate`;
the API never accepts arbitrary state changes.

Errors are accumulated during fixture loading as `Issue(code, path, message)` values. A
missing task binding, bad fingerprint, unknown role, or malformed policy does not abort the
loader; the caller receives an issue list and refuses to seed an invalid registry.

## 2. State machine

```text
                       promote
              +--------------------------+
              |                          v
        +-----------+   rotate    +----------+   finalize   +-----------+
        |  standby  |------------>| rotating |------------->|  active   |
        +-----------+             +----------+              +-----------+
           |   ^                      |   |                       |   |
           |   |       finalize       |   +---------------------->|   |
           |   +----------------------+                          |   |
           |                                                      |   |
           |  demote / expiry                                     | expiry
           v                                                      v
        +-----------+                                       +-----------+
        |  expired  |<--------------------------------------|  standby  |
        +-----------+                                       +-----------+
             |
             | rotate (simulated renewal)
             +------------------> rotating -> active/standby

  Any non-revoked state --revoke--> revoked (terminal)
```

Transition whitelist (source of truth in `state_machine.py`):

| From | Allowed to |
| --- | --- |
| `active` | `standby`, `rotating`, `expired`, `revoked` |
| `standby` | `active`, `rotating`, `expired`, `revoked` |
| `rotating` | `active`, `standby`, `expired`, `revoked` |
| `expired` | `rotating`, `revoked` |
| `revoked` | none |

`expired` is clock-driven: `StateMachine.expire_if_due` compares `expires_at` to the injected
clock. An operator cannot manually force `expired`; a rejected attempt raises
`InvalidTransition` and writes a `credential.transition_rejected` audit event. Same-state
transitions are illegal. Revoked is terminal.

Effective state is computed without waiting for a write:

```text
effective_state(now) =
    revoked                      if stored == revoked
    expired                      if stored == expired or now >= expires_at
    stored state                 otherwise
```

The resolver and read APIs use effective state. The rotator reconciles an overdue stored
state to `expired` inside the rotation transaction before renewing it.

## 3. Plan algorithm

`build_plan(credentials, tasks, policy, now)` is pure: it performs no I/O and does not read
the wall clock.

```text
1. Normalize now to UTC and index credentials/tasks.
2. Sort tasks by (priority, task_id).
3. For each credential, sorted by ID:
     effective = credential.effective_state(now)
     skip revoked and already-rotating credentials
     if effective == expired:              reason = EXPIRED, urgency 0
     else if 0 <= days_left <= critical:   reason = CRITICAL_EXPIRY, urgency 1
     else if 0 <= days_left <= horizon:    reason = EXPIRING_SOON, urgency 2
     else:                                 no rotation item
     target = active for stored active credentials, otherwise standby
     affected_tasks = tasks binding it, matching source and required role
     emit rotate(item)
4. For each task and each required role, in task order:
     count final-state active candidates
     while count < min_active:
         choose the best standby by binding rank/ID
         upgrade the existing rotate item to target active, or emit promote(item)
         urgency = REDUNDANCY_LOW (3)
     if coverage still short: record task:role in active_deficits
5. Compute standby coverage for every task; record
   max(0, backup_margin - standby) in backup_deficits. This is an observation:
   a plan cannot invent credentials.
6. Sort items by (urgency, minimum task priority, credential_id).
7. Truncate to policy.max_items_per_plan and record the truncation count.
8. Derive batch ID from format_time(now) plus normalized item keys.
```

The planner guarantees a stable order and uses integer day counts. If several credentials
share an urgency and task priority, credential ID breaks the tie. `backup_margin` is used
both to surface unresolved redundancy and to preserve a visible deficit; `min_active` may
upgrade an expiring standby's target from standby to active.

`batch_id = BATCH-<YYYYMMDDTHHMMSSZ>-<first 10 hex chars of sha256(payload)>`, where
`payload` contains the normalized timestamp and item
`(credential_id, action, target_state, reason_code)` tuples. No random or process-local data
is included.

## 4. Rotation semantics

A `rotate` item simulates a metadata renewal with a fresh synthetic identity:

```text
reconcile expiry (clock actor) if needed
current state --rotate--> rotating
version += 1
seq = smallest unoccupied SYNTH sequence for (source, role)
synthetic_ref = SYNTH-<source>-<role>-<seq>
fingerprint = sha256(synthetic_ref)
issued_at = now
expires_at = now + policy.rotation_period_days
rotating --finalize--> planned target (active or standby)
```

The new `seq` is the smallest positive integer not occupied by any existing credential
with the same source and role, scanned inside the rotation transaction; revoked records
keep their sequence numbers burned. Before anything is persisted, the renewed identity is
validated against the registry rules (`rotation_identity` in `model.py`): the
`synthetic_ref` must match `SYNTH-<source>-<role>-<seq>` and must not collide with any
other credential's ref (`IDENTITY_CONFLICT`), the fingerprint must equal
`sha256(synthetic_ref)` (`FINGERPRINT_MISMATCH`), and the validity window must be exactly
`now` .. `now + rotation_period_days` (`VALIDITY_MISMATCH`). Any violation aborts the
whole batch — no half-updated version is persisted — and the failure is recorded with a
structured `[CODE]` reason. A `promote` item transitions `standby -> active` without a
version bump or identity change. No new credential row is created.

**Transaction boundary.** All items for a batch execute in one `BEGIN IMMEDIATE` SQLite
transaction. Any exception rolls back every credential update and every rotation item. After
rollback, the executor writes:

- batch status `failed`, execution timestamp, and `failure_reason`;
- a failed rotation item with `ok = 0`;
- a `rotation.failed` audit event.

The failed batch can be retried after the cause is corrected. On success, status becomes
`completed`, all items have `result=executed`, `ok=1`, and a `rotation.completed` audit is
written in the same transaction.

**Idempotency.** A completed batch is not re-executed. Calling rotate again returns the plan
items with `result=skipped`, `ok=1`, `executed=0`, and no state changes. Empty plans complete
cleanly. Promotion items that already reached `active` at execution time are marked skipped.

## 5. Verification and resolution

`verifier.verify_all` looks at every effective active/standby credential, calls only
`http://127.0.0.1:<port>/verify`, and persists one observation per credential. The URL
validator rejects any host other than `127.0.0.1`, any scheme other than HTTP, credentials,
query strings, and fragments. Session proxies are disabled (`trust_env=False`).

Stable result codes:

| Condition | Outcome | Code |
| --- | --- | --- |
| Stub says valid | `valid` | `VALID` |
| Stub says invalid | `invalid` | `INVALID` |
| Stub says unknown | `unknown` | `UNKNOWN` |
| Read timeout | `error` | `TIMEOUT` |
| Connect timeout/refused | `error` | `STUB_UNREACHABLE` |
| Non-200 / bad JSON / bad outcome | `error` | `STUB_HTTP_*`, `STUB_BAD_JSON`, `STUB_BAD_OUTCOME` |

Batch conclusion:

- `healthy`: at least one check and every checked outcome is `valid`;
- `failing`: at least one check and no outcome is `valid`;
- `degraded`: mixed outcomes, errors, or no checks.

The resolver evaluates each bound candidate for each required role:

1. Filter to effective `active` or `standby`.
2. Require the latest observation to be fresh and `valid`
   (unless `resolver.allow_unverified` is explicitly enabled, which labels the selection
   unverified and degraded).
3. Rank usable candidates by active-before-standby, binding rank, newest version, credential
   ID.
4. Select the best candidate per role; a standby selection marks the result `degraded`.
5. If a role has no candidate, return `NO_BINDINGS`, `NO_MATCHING_ROLE`, or
   `NO_HEALTHY_CANDIDATE` with every attempt and failure reason. No bare exception escapes.

Resolution failures are audited (`resolution.failed`); reports call the resolver with audit
writes disabled so read-only rendering cannot mutate the ledger.

## 6. Audit model

Every audit row has `at`, `actor`, `action`, `subject`, `before_json`, `after_json`,
`batch_id`, and `reason`. Relevant actions:

- `credential.transition` and `credential.transition_rejected`;
- `credential.renewed`;
- `rotation.completed` and `rotation.failed`;
- `verification.checked` and `verification.batch`;
- `resolution.failed`.

Audit rows are append-only in application code. State-machine audits can join the caller's
SQLite transaction by supplying a connection; a rolled-back rotation therefore does not keep
its in-transaction events. Failure evidence is explicitly re-written after rollback.

## 7. Reports and archives

`build_report` reads credentials, tasks, latest observations, one batch, rotation items,
failures, and resolver results using the injected clock. It emits:

- credential overview with stored and effective state;
- rotation batch summary and item outcomes;
- health matrix and conclusion;
- per-task resolvability;
- deduplicated failure details from tasks, batches, and failure audit events.

JSON is rendered with `sort_keys=True` and sorted arrays. Markdown is generated solely from
the JSON payload. No latency, auto-increment row ID, or wall-clock value appears. With a
fixed clock and unchanged ledger, both formats are byte-identical across runs.

`archive_batch` exports the selected batch, its plan, rotation items, batch-scoped
verifications, batch-scoped audits, and current credential metadata. It is an export
snapshot, not a historical time-travel database.

## 8. Persistence

SQLite uses WAL mode for file databases. Each unit of work opens a connection; writes use
explicit transactions. The schema stores no secret material. The API and CLI share the same
repositories, so behavior does not fork between surfaces.
