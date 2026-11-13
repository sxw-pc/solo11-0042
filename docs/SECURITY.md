# credrot Security and Compliance

## COMPLIANCE STATEMENT — READ FIRST

`credrot` is intended only for managing credential **metadata** belonging to the operator
or to local collection tasks the operator is explicitly authorized to run. It must not be
used to access, test, enumerate, or interfere with systems for which authorization has not
been granted. The operator is solely responsible for complying with applicable law,
contracts, data-source terms, and organizational policy. **Do not use this project against
unauthorized systems.**

The repository contains synthetic values only. There are no real secrets, company names,
private endpoints, or target domains. All fictional sources are named `DS-ALPHA`,
`DS-BETA`, and `DS-GAMMA`; all synthetic references start with `SYNTH-`. The verification
stub listens only on `127.0.0.1` and cannot be pointed at an external host by the shipped
client.

## 1. Data-handling boundary

### Stored

- Credential IDs, synthetic source/role labels, version numbers.
- Synthetic placeholders shaped `SYNTH-<source>-<role>-<seq>`.
- A SHA-256 fingerprint of that **synthetic placeholder**.
- Issued/expiry timestamps and lifecycle states.
- Task metadata and ordered credential bindings.
- Rotation plans, per-item outcomes, verification verdicts, and audit events.

### Not stored, not processed

- Real passwords, API keys, access tokens, private keys, session cookies, or secret material
  in any form.
- Encryption keys, KMS/HSM material, or secret derivation material.
- Real credential plaintext or ciphertext.
- Data returned by a real data source.

The fingerprint is an integrity label for a public synthetic placeholder. It is not a secret
hash of a real credential and must not be treated as a password hash or as protection for
one. Do not replace the synthetic placeholder with a real secret; the schema and tooling are
not designed for secret storage.

## 2. Network behavior

- Running tests, `init-demo`, `plan`, `rotate`, `report`, `archive`, `resolve`, and the home
  page performs no external network I/O.
- `verifier.py` accepts only `http://127.0.0.1[:port]` and disables environment proxy trust
  for its session. It rejects other hosts, HTTPS, userinfo, queries, and fragments **before**
  sending a request.
- The stub server enforces the loopback bind address. Its only routes are `GET /healthz` and
  `POST /verify`; it returns a verdict, a stable code, and a constant marker. It does not
  read or return secret material.
- Flask serving is restricted by the CLI to `--host 127.0.0.1`. If you write your own
  launcher, do not expose the app to a network interface without an authentication and
  authorization design that is outside this project's scope.
- Dependency installation is the only intended external network operation.

## 3. Stub semantics

The stub is a deterministic local stand-in, not a credential validator. It maps a synthetic
fingerprint to one of:

- `valid` — synthetic health is good;
- `invalid` — synthetic health is bad;
- `unknown` — no rule matched.

Unknown is a deliberate fail-closed outcome for resolution: a credential without a fresh
`valid` observation is not selected. Controllable per-rule delays exist to exercise timeout
behavior. They are not a defense against timing attacks and should not be used to emulate a
real security control.

## 4. Auditing and integrity

- State changes, rejected transitions, rotations, verification observations, and resolution
  failures are appended to the `audit_events` table with actor, subject, before/after JSON,
  batch ID, reason, and UTC timestamp.
- Rotation executes in one SQLite transaction. A failure rolls back all credential changes;
  failure metadata and a failure audit are written afterward.
- Audit rows are append-only at the application layer. The project does not implement
  cryptographic tamper evidence, signed logs, remote log shipping, or WORM storage.
- Database-level access control is the host filesystem's responsibility. Anyone who can
  write the SQLite file can alter metadata and audits.

## 5. Threat model and limitations

`credrot` addresses operational mistakes: stale metadata, ambiguous ownership, untested
fallbacks, non-idempotent manual rotations, and missing evidence. It does **not** defend
against:

- a compromised local account or host;
- theft of the SQLite database (metadata/audit confidentiality is not encrypted at rest);
- malicious insiders who can modify fixtures, the ledger, or the code;
- a compromised Python/Flask/requests/PyYAML dependency;
- real credential leakage introduced by an operator into fixtures or free-text reasons;
- network exposure created by a nonstandard launcher;
- authorization failures in downstream systems that consume the metadata.

The tool is not a KMS, vault, SIEM, DLP product, or compliance certification. Encryption at
rest, HSM/KMS integration, secret issuance, multi-user authorization, approvals, and
distributed audit are explicitly out of scope.

## 6. Operator responsibilities

1. Use only credentials and tasks you own or are authorized to manage.
2. Keep real secrets entirely outside this repository and outside credrot fixtures.
3. Run the service and stub on loopback only; keep the host patched and access-controlled.
4. Review generated plans before invoking `rotate`. The plan is the authorization unit;
   `POST /api/rotate` accepts only an existing batch ID.
5. Protect SQLite files and archive/report exports as internal operational records. They
   contain metadata and audit history, not secret values, but may reveal task topology.
6. Preserve evidence before restoring a database backup. Rotation is forward-only; manual
   rollback uses a pre-batch database copy.
7. Treat `unknown`, stale, invalid, and error verdicts as unavailable. Do not silently
   bypass the resolver's fail-closed behavior outside an approved procedure.
8. Review dependencies and pin them in your own environment if required by local policy.

## 7. Prohibited use

- Accessing or testing systems without explicit authorization.
- Storing, deriving, or transmitting real credentials through fixtures, metadata fields,
  audit reasons, reports, or the stub.
- Exposing the Flask app or stub beyond loopback as a substitute for authentication.
- Using the project for denial-of-service, credential stuffing, scanning, or evasion.
- Removing the loopback validation or synthetic-data convention while claiming this
  project's safety properties.

## 8. Cleanup and retention

- `init-demo` clears the selected ledger. Back up first if evidence is needed.
- Runtime data lives under `.credrot/`; report exports live under `reports/` by default.
  Both are git-ignored.
- Archive snapshots and reports should follow the operator's retention policy. Delete
  runtime databases and exports when no longer needed.
- No credential material is intentionally present, but if a real secret is accidentally
  introduced, treat the ledger, backups, reports, and shell history as compromised, rotate
  the affected credential in its authoritative system, and remove the data.

## 9. Reporting security concerns

This is an offline engineering sample without a production security response process. If
you adapt it, establish an internal owner, a private reporting channel, and a review process
before using it with any non-synthetic metadata.
