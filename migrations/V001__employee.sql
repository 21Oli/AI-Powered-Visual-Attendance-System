-- Phase 3, Step 1.5b — Migration V001: employee metadata table.
--
-- Static SQL only, per the approved Step 1.5b contract (OQ-4):
--   * no parameter substitution, no procedural branching;
--   * no CREATE DATABASE and no CREATE INDEX CONCURRENTLY (both are
--     prohibited here: neither can run inside the runner's migration
--     transaction, and CREATE DATABASE is owner-provisioned, never
--     migration-owned);
--   * executed through the Step 1.5b :mod:`src.db_migrate` runner, one
--     file per transaction, on a positively verified target.
--
-- Payload is employee *metadata only* (approved decision D1 in
-- DB_DESIGN.md): no biometric columns, no embeddings, no
-- attendance-event tables ever enter PostgreSQL in this phase.
--
-- ``created_at`` has deliberately NO database default (approved D2):
-- the value is application-authored from ``datetime.now(UTC)`` and the
-- repository inserts it verbatim. A default would silently substitute
-- a different (session) timestamp the application did not author.

-- Approved employee table contract (DB_DESIGN.md Section 2, verbatim):
CREATE TABLE employee (
    employee_id TEXT PRIMARY KEY,
    full_name   TEXT NOT NULL,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL          -- application-authored; NO default
);
