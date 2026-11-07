-- ==========================================================================
-- Owner-run setup for the dedicated, disposable Tier-2 PostgreSQL database
-- target database: visual_attendance_tier2_test
--
-- THIS IS NOT A MIGRATION. It does not live in migrations/ (the migration
-- loader scans every *.sql there and would refuse this file's name), and it
-- is never executed by any automated gate. It is a one-time, owner-reviewed
-- provisioning script for a disposable database.
--
-- Prerequisites (all owner actions, outside this script):
--   * the database itself must already exist: CREATE DATABASE is deliberately
--     NOT included here, because it cannot run inside a transaction and the
--     repository treats database creation as owner-provisioned, never
--     migration-owned;
--   * this file must be executed CONNECTED TO visual_attendance_tier2_test,
--     never to the application database. The precondition block below aborts
--     if the connected database is not that exact name;
--   * the directory/version of PostgreSQL is owner-chosen: the repository
--     pins no server version. psycopg 3 (the project's driver) is required
--     at run time, not by this script;
--   * the runtime role named in section 7, attendance_tier2_runtime, must
--     already exist as an ordinary non-superuser LOGIN role: this script
--     never creates roles and never grants role-level attributes.
--
-- What this script establishes (all inside ONE transaction):
--   1. the migration ledger table schema_version (documented shape,
--      DB_DESIGN.md Section 3.1; the runner's own ensure_ledger is the only
--      production writer of it, and no psycopg-backed executor exists in the
--      repository yet, so this step is necessarily manual);
--   2. the exact contents of migrations/V001__employee.sql (verbatim below);
--   3. the version-1 ledger row carrying that file's verified canonical
--      SHA-256 checksum, so a future runner run accepts the row instead of
--      refusing it as checksum drift;
--   4. the _phase3_test_guard table (DB_DESIGN.md Section 5.3);
--   5. exactly one marker row: VISUAL_ATTENDANCE_PHASE3_TEST_ONLY, with
--      schema_version = 1 matching the ledger;
--   6. post-setup verification that fails the whole transaction if anything
--      is not exactly as required.
--
-- Safety properties:
--   * no CREATE DATABASE, no DROP, no TRUNCATE, no ALTER;
--   * no credentials, DSNs or host names appear anywhere in this file;
--   * existing objects are a hard stop: the precondition block refuses if
--     employee, schema_version or _phase3_test_guard already exist, so
--     nothing is silently overwritten or "repaired";
--   * grants are a separate, clearly marked section at the bottom and are
--     deliberately minimal (derived from the Tier-2 harness's actual SQL).
--
-- How to run (any client; examples for psql):
--   psql -v ON_ERROR_STOP=1 --dbname "$ATTENDANCE_TEST_DATABASE_URL" \
--        --file tools/setup_tier2_test_database.sql
--   ON_ERROR_STOP is required: without it psql continues after a failed
--   statement. The script's own exception blocks abort the transaction, so
--   a failure leaves the database exactly as it was.
-- ==========================================================================

BEGIN;

-- Pin the schema every unqualified statement below resolves to: the
-- precondition checks and the verification block address public.*
-- explicitly, so the objects this script creates must land there too (a
-- session whose search_path puts another schema first is a hard stop
-- instead of a silent relocation). SET LOCAL is transaction-scoped, so
-- nothing outside this transaction inherits it.
SET LOCAL search_path = public;

-- --------------------------------------------------------------------------
-- 0. Preconditions: exact target, and no conflicting pre-existing objects
-- --------------------------------------------------------------------------
DO $$
DECLARE
    v_db          text := current_database();
    v_owner       text;
BEGIN
    IF v_db IS DISTINCT FROM 'visual_attendance_tier2_test' THEN
        RAISE EXCEPTION
            'refusing to run: connected to database %, expected visual_attendance_tier2_test',
            v_db;
    END IF;

    IF to_regclass('public.schema_version') IS NOT NULL THEN
        RAISE EXCEPTION
            'refusing to run: schema_version already exists (provisioning is one-time; review the existing ledger instead of re-creating it)';
    END IF;
    IF to_regclass('public.employee') IS NOT NULL THEN
        RAISE EXCEPTION
            'refusing to run: employee already exists (schema outside this script is not re-derived or repaired)';
    END IF;
    IF to_regclass('public._phase3_test_guard') IS NOT NULL THEN
        RAISE EXCEPTION
            'refusing to run: _phase3_test_guard already exists (existing marker evidence is never overwritten)';
    END IF;

    -- Informational only: the operator should confirm this is the disposable
    -- test database's owner role. Creating these objects needs CREATE on the
    -- schema; the GRANT section at the bottom needs ownership (or grant
    -- option) on the objects and the database.
    SELECT pg_get_userbyid(datdba) INTO v_owner
      FROM pg_database WHERE datname = v_db;
    RAISE NOTICE 'provisioning %, database owner: %, running as: %',
        v_db, v_owner, current_user;
END
$$;

-- --------------------------------------------------------------------------
-- 1. Migration ledger (documented shape: DB_DESIGN.md Section 3.1)
--    One row per applied migration; the runner materializes this table in
--    production via its executor's ensure_ledger (no such executor exists in
--    the repository yet). Column shape is fixed by contract:
--      version    INTEGER NOT NULL PRIMARY KEY
--      checksum   TEXT    NOT NULL   (sha256 of the applied file's content)
--      applied_at TIMESTAMPTZ NOT NULL
-- --------------------------------------------------------------------------
CREATE TABLE schema_version (
    version    INTEGER NOT NULL PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL
);

-- --------------------------------------------------------------------------
-- 2. Exact contents of migrations/V001__employee.sql, reproduced verbatim.
--    (Bytes are unchanged; the checksum recorded in step 3 is of that file,
--    not of this script.)
-- --------------------------------------------------------------------------
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

-- --------------------------------------------------------------------------
-- 3. The version-1 ledger row.
--    Checksum = SHA-256 of migrations/V001__employee.sql's canonical text
--    (UTF-8, BOM stripped, CRLF/CR normalized to LF), verified against the
--    file in this repository. applied_at must be timezone-aware.
-- --------------------------------------------------------------------------
INSERT INTO schema_version (version, checksum, applied_at)
VALUES (
    1,
    '9e6ac6b5d1e0fd016a99c007ec5b91070dd480853d54d37cdab5b069dd2deac9',
    now()
);

-- --------------------------------------------------------------------------
-- 4. Guard marker table (DB_DESIGN.md Section 5.3; owner-established, which
--    is why it is not part of any migration). Columns are exactly the three
--    the guard reads, in the order it reads them.
-- --------------------------------------------------------------------------
CREATE TABLE _phase3_test_guard (
    marker         TEXT        NOT NULL UNIQUE,
    created_at     TIMESTAMPTZ NOT NULL,
    schema_version INTEGER     NOT NULL
);

-- --------------------------------------------------------------------------
-- 5. Exactly one marker row. schema_version must equal the ledger's
--    MAX(version) = 1; created_at must be timezone-aware (TIMESTAMPTZ).
-- --------------------------------------------------------------------------
INSERT INTO _phase3_test_guard (marker, created_at, schema_version)
VALUES ('VISUAL_ATTENDANCE_PHASE3_TEST_ONLY', now(), 1);

-- --------------------------------------------------------------------------
-- 6. Post-setup verification. Any deviation raises and rolls back the whole
--    transaction: no partially provisioned database is ever left behind.
-- --------------------------------------------------------------------------
DO $$
DECLARE
    v_count              integer;
    v_version            integer;
    v_checksum           text;
    v_applied_at         timestamptz;
    v_marker             text;
    v_marker_created_at  timestamptz;
    v_marker_version     integer;
    v_pk_ok              boolean;
BEGIN
    -- 6a. employee: exactly the four approved columns with the approved
    --     types/nullability/defaults.
    SELECT count(*) INTO v_count
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'employee';
    IF v_count <> 4 THEN
        RAISE EXCEPTION 'employee must have exactly 4 columns, found %', v_count;
    END IF;

    SELECT count(*) INTO v_count
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'employee'
       AND (   (column_name = 'employee_id'
                AND data_type = 'text'
                AND is_nullable = 'NO')
            OR (column_name = 'full_name'
                AND data_type = 'text'
                AND is_nullable = 'NO')
            OR (column_name = 'is_active'
                AND data_type = 'boolean'
                AND is_nullable = 'NO'
                AND column_default = 'true')
            OR (column_name = 'created_at'
                AND data_type = 'timestamp with time zone'
                AND is_nullable = 'NO'
                AND column_default IS NULL));
    IF v_count <> 4 THEN
        RAISE EXCEPTION
            'employee column definitions differ from migrations/V001__employee.sql';
    END IF;

    -- 6b. employee primary key is exactly employee_id.
    SELECT EXISTS (
        SELECT 1
          FROM pg_index i
          JOIN pg_class c     ON c.oid = i.indrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public'
           AND c.relname = 'employee'
           AND i.indisprimary
           AND i.indisunique
           AND i.indnatts = 1
           AND (SELECT a.attname
                  FROM pg_attribute a
                 WHERE a.attrelid = c.oid
                   AND a.attnum = i.indkey[0]) = 'employee_id'
    ) INTO v_pk_ok;
    IF NOT v_pk_ok THEN
        RAISE EXCEPTION 'employee primary key must be exactly (employee_id)';
    END IF;

    -- 6c. Ledger: exactly one row, version 1, the verified checksum, and a
    --     timezone-aware applied_at.
    SELECT count(*) INTO v_count FROM schema_version;
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'schema_version must contain exactly 1 row, found %', v_count;
    END IF;

    SELECT version, checksum, applied_at
      INTO v_version, v_checksum, v_applied_at
      FROM schema_version;

    IF v_version <> 1 THEN
        RAISE EXCEPTION 'schema_version must record version 1, found %', v_version;
    END IF;
    IF v_checksum IS DISTINCT FROM
       '9e6ac6b5d1e0fd016a99c007ec5b91070dd480853d54d37cdab5b069dd2deac9' THEN
        RAISE EXCEPTION
            'schema_version checksum does not match migrations/V001__employee.sql';
    END IF;
    IF v_applied_at IS NULL THEN
        RAISE EXCEPTION 'schema_version.applied_at must not be null';
    END IF;

    -- 6d. Marker: exactly one row, exact literal, ledger-matching version,
    --     tz-aware created_at (guaranteed by its column type).
    SELECT count(*) INTO v_count FROM _phase3_test_guard;
    IF v_count <> 1 THEN
        RAISE EXCEPTION
            '_phase3_test_guard must contain exactly 1 row, found %', v_count;
    END IF;

    SELECT marker, created_at, schema_version
      INTO v_marker, v_marker_created_at, v_marker_version
      FROM _phase3_test_guard;

    IF v_marker IS DISTINCT FROM 'VISUAL_ATTENDANCE_PHASE3_TEST_ONLY' THEN
        RAISE EXCEPTION '_phase3_test_guard.marker is not the approved literal';
    END IF;
    IF v_marker_created_at IS NULL THEN
        RAISE EXCEPTION '_phase3_test_guard.created_at must not be null';
    END IF;
    IF v_marker_version IS DISTINCT FROM (SELECT MAX(version) FROM schema_version) THEN
        RAISE EXCEPTION
            '_phase3_test_guard.schema_version (%) does not equal the ledger MAX(version)',
            v_marker_version;
    END IF;

    -- 6e. schema_version: exactly the three documented columns, with the
    --     approved types/nullability and no unexpected defaults.
    SELECT count(*) INTO v_count
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'schema_version';
    IF v_count <> 3 THEN
        RAISE EXCEPTION
            'schema_version must have exactly 3 columns, found %', v_count;
    END IF;

    SELECT count(*) INTO v_count
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'schema_version'
       AND (   (column_name = 'version'
                AND data_type = 'integer'
                AND is_nullable = 'NO'
                AND column_default IS NULL)
            OR (column_name = 'checksum'
                AND data_type = 'text'
                AND is_nullable = 'NO'
                AND column_default IS NULL)
            OR (column_name = 'applied_at'
                AND data_type = 'timestamp with time zone'
                AND is_nullable = 'NO'
                AND column_default IS NULL));
    IF v_count <> 3 THEN
        RAISE EXCEPTION
            'schema_version column definitions differ from DB_DESIGN.md Section 3.1';
    END IF;

    -- 6f. _phase3_test_guard: exactly the three columns the guard reads,
    --     in the approved types, all NOT NULL, no unexpected defaults.
    SELECT count(*) INTO v_count
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = '_phase3_test_guard';
    IF v_count <> 3 THEN
        RAISE EXCEPTION
            '_phase3_test_guard must have exactly 3 columns, found %', v_count;
    END IF;

    SELECT count(*) INTO v_count
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = '_phase3_test_guard'
       AND (   (column_name = 'marker'
                AND data_type = 'text'
                AND is_nullable = 'NO'
                AND column_default IS NULL)
            OR (column_name = 'created_at'
                AND data_type = 'timestamp with time zone'
                AND is_nullable = 'NO'
                AND column_default IS NULL)
            OR (column_name = 'schema_version'
                AND data_type = 'integer'
                AND is_nullable = 'NO'
                AND column_default IS NULL));
    IF v_count <> 3 THEN
        RAISE EXCEPTION
            '_phase3_test_guard column definitions differ from the guard contract';
    END IF;

    RAISE NOTICE
        'provisioning verified: employee(4 columns, pk=employee_id), ledger v1, marker v1';
END
$$;

COMMIT;

-- ==========================================================================
-- 7. Runtime grants — a SECOND, SEPARATE step. Run only AFTER the setup
--    transaction above has committed. These statements are deliberately
--    outside that transaction: they change privileges, not schema. A
--    failure here leaves a correctly provisioned database whose grants are
--    still to fix, and section 7b then raises on any missing privilege
--    instead of letting the first Tier-2 run discover it.
--
-- attendance_tier2_runtime is the dedicated, least-privilege runtime
-- role named by ATTENDANCE_TEST_DATABASE_URL. Every statement below,
-- including section 7b, targets exactly that role — never a superuser
-- role, whose bypass would make these grants and checks meaningless. Run
-- this as the database/object owner, or a role holding the objects WITH
-- GRANT OPTION. Every object name is schema-qualified, so this section
-- does not depend on the session search_path.
--
-- Derivation (from the actual SQL the Tier-2 harness executes):
--   * guard: SELECT on _phase3_test_guard and schema_version (its only reads
--     besides current_database(), which needs no grant);
--   * repository create: INSERT on employee;
--   * repository get/list: SELECT on employee;
--   * harness scenario test_missing_employee_update_is_typed_and_does_not_write
--     calls update_full_name, which executes
--     'UPDATE employee SET full_name = %s WHERE employee_id = %s RETURNING ...'
--     -> UPDATE on the column full_name only. Column-level scoping is enough;
--     the repository's deactivate() (which writes is_active) is never invoked
--     by Tier-2, so no UPDATE on is_active is granted;
--   * guarded cleanup: DELETE on employee (single bound-parameter DELETE).
-- No CREATE, no ALTER, no TRUNCATE, no DROP, no superuser.
-- ==========================================================================

-- GRANT CONNECT ON DATABASE visual_attendance_tier2_test
--     TO attendance_tier2_runtime;
--   ^ only needed if CONNECT was revoked from PUBLIC on this database.
GRANT USAGE ON SCHEMA public TO attendance_tier2_runtime;
GRANT SELECT ON public._phase3_test_guard TO attendance_tier2_runtime;
GRANT SELECT ON public.schema_version TO attendance_tier2_runtime;
GRANT SELECT, INSERT ON public.employee TO attendance_tier2_runtime;
GRANT UPDATE (full_name) ON public.employee TO attendance_tier2_runtime;
GRANT DELETE ON public.employee TO attendance_tier2_runtime;

-- --------------------------------------------------------------------------
-- 7b. Privilege verification for the runtime role. Fail closed: a missing
--     privilege (or a role that does not exist) raises here rather than at
--     the first Tier-2 run. Each privilege is checked separately because a
--     comma-separated has_table_privilege() list is satisfied by ANY of the
--     listed privileges, which would hide a gap. Note that has_*_privilege
--     also reports true through object ownership, so verify the actual
--     runtime role, not the provisioning/owner role.
-- --------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT has_schema_privilege(
           'attendance_tier2_runtime', 'public', 'USAGE'
       )
    THEN
        RAISE EXCEPTION 'runtime role lacks USAGE on schema public';
    END IF;

    IF NOT has_table_privilege(
           'attendance_tier2_runtime', 'public._phase3_test_guard', 'SELECT'
       )
    THEN
        RAISE EXCEPTION 'runtime role lacks SELECT on _phase3_test_guard';
    END IF;

    IF NOT has_table_privilege(
           'attendance_tier2_runtime', 'public.schema_version', 'SELECT'
       )
    THEN
        RAISE EXCEPTION 'runtime role lacks SELECT on schema_version';
    END IF;

    IF NOT has_table_privilege(
           'attendance_tier2_runtime', 'public.employee', 'SELECT'
       )
       OR NOT has_table_privilege(
           'attendance_tier2_runtime', 'public.employee', 'INSERT'
       )
       OR NOT has_table_privilege(
           'attendance_tier2_runtime', 'public.employee', 'DELETE'
       )
    THEN
        RAISE EXCEPTION
            'runtime role lacks one of SELECT/INSERT/DELETE on employee';
    END IF;

    IF NOT has_column_privilege(
           'attendance_tier2_runtime', 'public.employee', 'full_name', 'UPDATE'
       )
    THEN
        RAISE EXCEPTION 'runtime role lacks UPDATE on employee.full_name';
    END IF;

    RAISE NOTICE 'runtime grants verified for attendance_tier2_runtime';
END
$$;
