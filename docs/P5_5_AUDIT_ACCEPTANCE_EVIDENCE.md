# P5.5.8 acceptance evidence pack

**Scope.** This pack covers P5.5.8 only — audit journal verification and acceptance
preparation. It does not rehearse acceptance for other subsystems. Final Phase 5
acceptance is the owner's decision and is referenced here, not taken.

**Branch / HEAD.** `main`, `HEAD 012ae31aff2a9d28e1808659bb92866e5fe4a986` (owner committed P5.5.7).
Working tree at packaging time: `M CHANGELOG.md`, `M DEVELOPMENT_STATUS.md`,
`?? tests/test_audit_exclusion.py`, `?? tests/test_audit_failure_regression.py`,
`?? tests/test_audit_concurrency_soak.py`, plus the edits made by P5.5.8.5 to the
design / plan / architecture / changelog / status documents.

**No Git commit by the assistant.** No staging, commit, push, stash, reset or branch
change. This pack is preparatory evidence only.

---

## A — Acceptance date and scope boundary

- Acceptance recorded for this session: **2026-11-18**.
- Subsystem under review: **audit journal and security-event auditing (P5.5.8)**.
- Out of scope: P5.6 work, P5.9 (final Phase 5 acceptance), any other subsystem's
  acceptance, production data, real attendance / employee / biometric data, PostgreSQL,
  `.env` / credentials / secrets / DSNs, and any `data/audit/` directory.

## B — What is being accepted (and what is not)

This pack is evidence, **not acceptance**. It lists the evidence available and the
status of each item honestly:

- **PASS** — recorded as passing with the stated numbers.
- **NOT TESTED** — not exercised in this pack.
- **PENDING OWNER ACCEPTANCE** — done and evidenced, but the acceptance decision is
  reserved for the owner / P5.9.

## C — Suite results (recorded numbers only)

### C.1 — 15-file audit suite (P5.5.8 work included)

Command (run here, exit 0):

pytest tests/test_audit_schema.py
      tests/test_audit_writer.py
      tests/test_audit_service.py
      tests/test_api_audit.py
      tests/test_audit_verify.py
      tests/test_audit_manifest.py
      tests/test_audit_archive.py
      tests/test_audit_manifest_store.py
      tests/test_audit_rotation.py
      tests/test_audit_rotate_cli.py
      tests/test_audit_verify_history.py
      tests/test_audit_layout.py
      tests/test_audit_exclusion.py
      tests/test_audit_failure_regression.py
      tests/test_audit_concurrency_soak.py
-q

Result: **780 passed, 1 skipped in
90.23 s, exit 0.**

Notes:

- `tests/test_audit_concurrency_soak.py` is **5 tests**, added by P5.5.8.4.
- `tests/test_audit_exclusion.py` is **50 tests**, P5.5.8.2.
- `tests/test_audit_failure_regression.py` is **18 tests**, P5.5.8.3.
- The remaining audit-regression coverage inside the suite (P5.5.8.1 re-tie) totals **346 tests**.

### C.2 — Full backend suite (P5.5.8 included, plus the rest of the project)

Command: full pytest invocation against the project (exit 0), recorded to
`.freebuff/p558_456_full.log`.

Result: **3871 passed, 33 skipped in
365.59 s, exit 0.**

A rerun of the same full-suite invocation gave the same totals
(3871 passed, 33 skipped, exit 0) — the package
is self-consistent as packaged for review.

### C.3 — P5.5.8.4 soak timing

- Soak test file: **5 tests, 47.12 s** total.
- The 1000-append test alone: **~42.51 s**.
- The 12-cycle soak: **~2.67 s**.

## D — Traceability: P5.5.1 → P5.5.8.5

(format: milestone · recorded status · test scope / artifact · own tests)

| # | Status (recorded) | Test scope / artifact | Own tests |
|---|---|---|---|
| P5.5.1 | Design approved (recorded as such in the status log) | — | — |
| P5.5.2 | Primitives implemented, awaiting review | — | — |
| P5.5.3 | Writer implemented, awaiting review | — | — |
| P5.5.4 | Service implemented, awaiting review | — | — |
| P5.5.5 | Route integration implemented, awaiting review | — | — |
| P5.5.5.1 | Login entry-point audit integration implemented, awaiting review | — | — |
| P5.5.6 | Verifier CLI accepted (committed `87c73e5`) | — | — |
| P5.5.7.1 | Manifest schema / integrity accepted (committed `c2871f3`) | — | — |
| P5.5.7.2 | Manifest schema/integrity accepted (committed `c2871f3`) | — | — |
| P5.5.7 | Manifest persistence, rotation, recovery, retention and manifest-aware verification implemented, awaiting review | — | — |
| P5.5.8.1 | P5.5.8 foundational audit regression (re-tied into the 15-file audit suite as part of P5.5.8.3 acceptance evidence) | tests/test_audit_schema.py … (within the 15-file audit suite) | 346 |
| P5.5.8.2 | Exclusion sweep harness — implemented, awaiting review | tests/test_audit_exclusion.py | 50 |
| P5.5.8.3 | Failure and recovery regression — implemented, awaiting review | tests/test_audit_failure_regression.py | 18 |
| P5.5.8.4 | Bounded concurrency and rotation soak — implemented, awaiting review | tests/test_audit_concurrency_soak.py | 5 |
| P5.5.8.5 | Documentation reconciliation — implemented, awaiting review | Non-code: docs/AUDIT_JOURNAL_DESIGN.md §9.11/§12/§13, PHASE5_IMPLEMENTATION_PLAN.md §9.2/§9.7, ARCHITECTURE.md, DEVELOPMENT_STATUS.md, CHANGELOG.md | n/a |
| P5.5.8.6 | Acceptance evidence pack — prepared, awaiting independent review and owner acceptance | docs/P5_5_AUDIT_ACCEPTANCE_EVIDENCE.md (this file) | n/a |

### D.1 — How the soak exercises the milestones

- P5.5.8.4 exercises P5.5.8.1 (regression path still live) and P5.5.8.3 (the
  failure model is reused by the soak's poison and recovery scenarios).
- P5.5.8.5 does not have a test file; it is source-vs-document comparison plus
  `git diff --check`.

## E — Decisions exercised

The soak and the failure harness exercise, but do not add to the bug list:

- Single-writer assumption: the module-level `threading.Lock` serialises appends in
  one process; a second writer (or process not holding the lock) is refused and
  poisoned, not merged.
- OS byte-0 lock: Windows `msvcrt` / POSIX `flock` refuse a second process and
  poison it; a held lease can surface as `RotationUnavailableError` rather than
  `RotationContendedError` — both are accepted as correct.
- Rotation exclusivity: rotation acquires `LayoutLock` + journal lease; it refuses
  while a writer is active and leaves no history change.
- Recovery is not automatic: an unclean state (manifest store error, torn manifest
  tail, absent manifest) is reported through the stable exit codes and left for the
  documented recovery commands.
- Stable exit codes: 0 PASS, 1 integrity failure, 2 unavailable / changed during
  verification, 3 usage error, 4 internal error, 5 recovery required — defined once
  in `tools/audit_verify.py` / `tools/audit_rotate.py` and kept in sync with the
  design §11 table.

## F — Limitations of the evidence (honest)

- **Concurrency model boundary.** The soak is single-process, multi-thread, on a local
  filesystem only. No multi-process, distributed, network-filesystem or production-load
  guarantee is made by this pack. The concurrency model is documented in
  `docs/AUDIT_JOURNAL_DESIGN.md` §9.11; the soak verifies it under that stated boundary.
- **Windows mandatory byte locks.** Windows file locking is mandatory for other
  handles; a held lease can surface as `RotationUnavailableError` rather than
  `RotationContendedError`. The tests accept both, and the production contract
  documents both.
- **Flush / fsync ordering.** Because `flush` precedes `fsync`, an unconfirmed result
  may already be on disk when `AUDIT_RESULT_UNCERTAIN` is returned. This is the
  documented uncertain state, not a defect; the tests assert the certificate rather
  than an exact line count, and the soak allows `len(events) in (limit, limit+1)`.
- **Segment labels are allocation identifiers.** Segment filenames stay
  `2026-11-NNN` successors even after record timestamps cross into December. This is
  deliberate (design §9.7), tested and observed — not a defect.
- **`archive/` directory must exist.** `RotationOperations.rotate()` does not create
  `archive/`; the first rotation without it raises `RotationUnavailableError`. The tests
  pre-create it (as `install()` does). This is an implementation observation recorded
  here, not a defect being fixed in this pack.
- **No live data, no network, no secrets.** Everything here is synthetic, in `tmp_path`,
  offline, with no `.env` / credentials / DSNs / PostgreSQL / `data/audit/`.
- **Past `DEVELOPMENT_STATUS.md` Step records are historical as written.** Step 70 / 71 /
  72 records retain their as-written numbers; only live contract wording is corrected by
  P5.5.8.5.

## G — Guardrails checked

- `data/audit/` was **not** created, read or accessed during the soak or the full suite.
- No `.env`, credentials, secrets or DSNs were read or written.
- No production code in `src/` or `tools/` was changed by P5.5.8.4 or P5.5.8.6.
- P5.5.8.5 changed only documentation files (no production behaviour).
- `git diff --check` passes on the packaged tree (verified after writing this pack).

## H — Pre-packaging checklist (done)

- [x] Audit suite (15 files) passes with the P5.5.8.4 soak included: 780 passed, 1 skipped, exit 0.
- [x] Full backend suite passes: 3871 passed, 33 skipped, exit 0.
- [x] `tests/test_audit_concurrency_soak.py` is present and non-empty (5 tests).
- [x] `tests/test_audit_exclusion.py` is present and non-empty (50 tests).
- [x] `tests/test_audit_failure_regression.py` is present and non-empty (18 tests).
- [x] Documentation reconciled: exit codes, concurrency model, milestone sub-rows, scope.
- [x] `git diff --check` clean on the packaged tree.
- [x] No `data/audit/` created / read / accessed.
- [x] This evidence pack written (sections A–J) and non-empty.

## I — Acceptance checklist (owner)

This is the owner's acceptance list. Items below are **PENDING OWNER ACCEPTANCE**
unless marked PASS or NOT TESTED.

| # | Item | Status in this pack |
|---|------|---------------------|
| I.1 | P5.5.8.2 exclusion sweep harness is correct and complete | PENDING OWNER ACCEPTANCE (50 tests, PASS) |
| I.2 | P5.5.8.3 failure and recovery regression is correct and complete | PENDING OWNER ACCEPTANCE (18 tests, PASS) |
| I.3 | P5.5.8.4 concurrency / rotation soak is correct and within scope | PENDING OWNER ACCEPTANCE (5 tests, PASS) |
| I.4 | P5.5.8.5 documentation now matches the implemented behaviour | PENDING OWNER ACCEPTANCE (docs only, PASS) |
| I.5 | P5.5.8.6 evidence pack is honest and complete | PENDING OWNER ACCEPTANCE (this file) |
| I.6 | P5.5.7 (manifest persistence, rotation, recovery, retention, manifest-aware verification) accepted | PENDING OWNER ACCEPTANCE — recorded as awaiting review |
| I.7 | P5.5.2–P5.5.5 accepted | PENDING OWNER ACCEPTANCE — recorded as awaiting review |
| I.8 | P5.5.8.1 regression (re-tied into the suite) accepted | PENDING OWNER ACCEPTANCE |
| I.9 | Any open limitations the owner wants addressed before acceptance | PENDING OWNER DECISION |

## J — How to revisit

- To re-run the 15-file audit suite: the command in §C.1.
- To re-run the full suite: the full pytest invocation that produced §C.2, with results
  written to a log.
- To inspect the concurrency model: `src/audit_writer.py` (module-level `_PROCESS_LOCK`,
  `append`), `src/audit_rotation.py` (`rotate` order), `src/audit_manifest_store.py`
  (`ManifestStore.append`, `ManifestStoreError`, `ManifestCorruptError`), and
  `docs/AUDIT_JOURNAL_DESIGN.md` §9.11 / §11.
- To inspect the CLI exit codes: `tools/audit_verify.py` and `tools/audit_rotate.py`.
- To inspect the prior Step records: `DEVELOPMENT_STATUS.md` Steps 70–75.

---

Pack assembled: 2026-11-18. Status of the items in this pack remains as recorded in
`DEVELOPMENT_STATUS.md` (P5.5.8.4 / P5.5.8.5 **implemented, awaiting independent review**;
P5.5.8.6 **evidence prepared, awaiting independent review and owner acceptance**); nothing
here is marked accepted.
