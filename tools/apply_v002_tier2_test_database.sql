-- ==========================================================================
-- Owner-run application of migrations/V002__security.sql to the dedicated,
-- disposable Tier-2 PostgreSQL database.
--
-- target database: visual_attendance_tier2_test   (hard-checked below)
-- target cluster:  pinned by pg_control_system().system_identifier
--
-- THIS IS NOT A MIGRATION. It lives outside migrations/ (the loader scans
-- every *.sql there and would refuse this file's name), and no automated gate
-- ever executes it. It exists because the configured
-- ATTENDANCE_TEST_DATABASE_URL authenticates as attendance_tier2_runtime,
-- which has USAGE but no CREATE on schema public and owns none of the
-- existing objects. Applying this script therefore needs a role with CREATE
-- on schema public, and grant option on that schema only when the cleanup
-- role does not already hold USAGE there (step 4 skips the grant otherwise).
--
-- Least privilege
-- ---------------
-- attendance_tier2_runtime (the application and suite DSN) receives exactly
-- SELECT, INSERT and UPDATE on app_user and user_session: no DELETE, no
-- ownership, no DDL. The application never deletes a row (deactivation is an
-- UPDATE), so DELETE is not an application privilege.
--
-- Synthetic test rows are removed by a SEPARATE, test-only role,
-- attendance_tier2_cleanup, which gets DELETE on exactly those two tables and
-- column-level SELECT on user_id only (the column its
-- DELETE ... WHERE user_id = ANY(%s) statements need). No table-level read, no
-- INSERT or UPDATE, no other column, no other table, no DDL. Plus
-- USAGE on schema public only if it does not already hold it. Its credential
-- is supplied only through
-- ATTENDANCE_TIER2_CLEANUP_DATABASE_URL to the tier-2 suite's teardown, never
-- to application code. This script never creates roles: both roles must
-- already exist as ordinary non-superuser LOGIN roles (the owner creates
-- them; see docs/PHASE5_IMPLEMENTATION_PLAN.md section 10.1).
--
-- Why one transaction
-- -------------------
-- src.db_migrate's contract is "the DDL and its schema_version row commit
-- together" (one migration per transaction, applied_at timezone-aware,
-- checksum = SHA-256 of the file's canonical text). Two separate steps would
-- allow a committed app_user with no ledger row, which the runner refuses to
-- repair. This script reproduces
-- src.db_migrate_executor.PsycopgMigrationExecutor.apply_migration exactly:
--
--   BEGIN
--     <V002 verbatim, checksummed file>
--     INSERT INTO schema_version ...   -- same transaction
--     GRANT ...                        -- same transaction
--     verification (raises -> whole transaction rolls back)
--   COMMIT
--
-- Any failure leaves the database exactly as it was: no partial schema, no
-- ledger row, no grants.
--
-- How to run
-- ----------
-- From the repository root, with a client that honours an explicit BEGIN /
-- COMMIT and aborts on the first error (psql: -v ON_ERROR_STOP=1). The script
-- contains no client meta-commands and must be executed as ONE script, never
-- statement by statement:
--
--   psql -v ON_ERROR_STOP=1 \
--        --dbname "<owner DSN for visual_attendance_tier2_test>" \
--        --file tools/apply_v002_tier2_test_database.sql
--
-- Never point this at the application or any production database; the
-- precondition block refuses any database whose name is not exactly
-- visual_attendance_tier2_test.
--
-- Server identity
-- ---------------
-- The database name alone cannot distinguish two clusters that both hold a
-- visual_attendance_tier2_test database. The precondition below therefore
-- also requires the cluster's pg_control_system().system_identifier to equal
-- the value captured read-only from the guarded tier-2 target on 2026-11-19.
-- This is a cluster fingerprint, not a credential; it is not usable to
-- authenticate. Limitation: it identifies the cluster, not the network path
-- (inet_server_addr()/inet_server_port() are reported for the owner's benefit
-- but not enforced, because a legitimate owner session may arrive over a
-- different interface or a local socket). If the disposable cluster is
-- deliberately re-initialised, capture the new identifier read-only and
-- update the constant in the same reviewed change — never by guessing.
-- ==========================================================================

BEGIN;

-- Pin the schema the unqualified statements below resolve to. SET LOCAL is
-- transaction-scoped, so nothing outside this transaction inherits it.
SET LOCAL search_path = public;

-- --------------------------------------------------------------------------
-- 0. Preconditions: exact target, cluster identity, executing authority,
--    expected V001 baseline, no conflicts, required roles
-- --------------------------------------------------------------------------
DO $$
DECLARE
    v_db             text := current_database();
    v_rows           integer;
    v_version        integer;
    v_checksum       text;
    v_conflict       text;
    v_sysid          numeric;
    c_expected_sysid constant numeric := 7687968999847354228;
BEGIN
    -- 0a. Exact database name.
    IF v_db IS DISTINCT FROM 'visual_attendance_tier2_test' THEN
        RAISE EXCEPTION
            'refusing to run: connected to database %, expected visual_attendance_tier2_test',
            v_db;
    END IF;

    -- 0b. Cluster identity (pg_control_system() is readable by ordinary
    --     roles on this cluster; if it is not, this statement fails and the
    --     script refuses rather than guessing).
    SELECT system_identifier INTO v_sysid FROM pg_control_system();
    IF v_sysid IS DISTINCT FROM c_expected_sysid THEN
        RAISE EXCEPTION
            'refusing to run: server system_identifier % does not match the expected test-cluster identity % (verify the endpoint independently before proceeding)',
            v_sysid, c_expected_sysid;
    END IF;

    RAISE NOTICE
        'target confirmed: db=%, system_identifier=%, server_version_num=%, inet_server_addr=%, inet_server_port=%',
        v_db, v_sysid, current_setting('server_version_num'),
        COALESCE(inet_server_addr()::text, '<local socket>'), inet_server_port();

    -- 0c. Executing authority: never a least-privilege role, and it must be
    --     able to create in schema public. Table grants need ownership, which
    --     the creator has and which is re-asserted before the GRANT section.
    --     Grant option on the schema is required only when the cleanup role
    --     does not already hold USAGE there; that is decided (and enforced)
    --     in step 4, next to the grant itself.
    IF current_user IN ('attendance_tier2_runtime', 'attendance_tier2_cleanup') THEN
        RAISE EXCEPTION
            'refusing to run as least-privilege role %; use a migration/owner role',
            current_user;
    END IF;

    IF NOT has_schema_privilege(current_user, 'public', 'CREATE') THEN
        RAISE EXCEPTION
            'refusing to run: role % lacks CREATE on schema public and cannot create the V002 objects',
            current_user;
    END IF;

    -- 0d. The objects V002 creates must not exist in any form (including a
    --     stray index of the same name): CREATE TABLE / CREATE INDEX would
    --     fail anyway, but this reports the conflict explicitly.
    SELECT string_agg(c.relname, ', ' ORDER BY c.relname)
      INTO v_conflict
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public'
       AND c.relname IN (
           'app_user',
           'user_session',
           'app_user_pkey',
           'app_user_username_key_key',
           'user_session_pkey',
           'user_session_user_id_idx',
           'user_session_expires_at_idx'
       );
    IF v_conflict IS NOT NULL THEN
        RAISE EXCEPTION
            'refusing to run: V002 object names already exist in public (%). This script is one-time; inspect the existing schema instead of re-creating it.',
            v_conflict;
    END IF;

    -- 0e. The V001 baseline this migration continues must be exactly as
    --     provisioned: employee present, ledger recording only version 1 with
    --     the verified V001 checksum, guard marker agreeing with the ledger.
    IF to_regclass('public.employee') IS NULL THEN
        RAISE EXCEPTION
            'refusing to run: employee is missing; the V001 baseline was not provisioned by tools/setup_tier2_test_database.sql';
    END IF;

    IF to_regclass('public.schema_version') IS NULL THEN
        RAISE EXCEPTION 'refusing to run: the migration ledger schema_version is missing';
    END IF;

    SELECT count(*) INTO v_rows FROM schema_version;
    IF v_rows <> 1 THEN
        RAISE EXCEPTION
            'refusing to run: schema_version must hold exactly the V001 row, found % row(s)',
            v_rows;
    END IF;

    SELECT version, checksum INTO v_version, v_checksum
      FROM schema_version
     WHERE version = 1;
    IF v_version IS NULL THEN
        RAISE EXCEPTION 'refusing to run: the V001 ledger row is missing';
    END IF;
    IF v_checksum IS DISTINCT FROM
       '9e6ac6b5d1e0fd016a99c007ec5b91070dd480853d54d37cdab5b069dd2deac9' THEN
        RAISE EXCEPTION
            'refusing to run: the V001 ledger checksum does not match the verified file checksum (schema drift)';
    END IF;

    IF to_regclass('public._phase3_test_guard') IS NULL THEN
        RAISE EXCEPTION 'refusing to run: the Tier-2 guard marker table is missing';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM _phase3_test_guard
         WHERE marker = 'VISUAL_ATTENDANCE_PHASE3_TEST_ONLY'
           AND schema_version = 1
    ) THEN
        RAISE EXCEPTION
            'refusing to run: the Tier-2 guard marker does not record schema version 1';
    END IF;

    -- 0f. Both runtime roles must pre-exist as ordinary non-superuser LOGIN
    --     roles, and must not be members of each other (membership would
    --     hand the application role DELETE, or the cleanup role write access).
    IF NOT EXISTS (
        SELECT 1 FROM pg_roles
         WHERE rolname = 'attendance_tier2_runtime'
           AND rolcanlogin
           AND NOT rolsuper
    ) THEN
        RAISE EXCEPTION
            'refusing to run: attendance_tier2_runtime must exist as a non-superuser LOGIN role';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_roles
         WHERE rolname = 'attendance_tier2_cleanup'
           AND rolcanlogin
           AND NOT rolsuper
           AND NOT rolcreaterole
           AND NOT rolcreatedb
    ) THEN
        RAISE EXCEPTION
            'refusing to run: attendance_tier2_cleanup must exist as a non-superuser LOGIN role without CREATEROLE/CREATEDB (create it first; this script never creates roles)';
    END IF;

    IF pg_has_role('attendance_tier2_cleanup', 'attendance_tier2_runtime', 'MEMBER')
       OR pg_has_role('attendance_tier2_runtime', 'attendance_tier2_cleanup', 'MEMBER') THEN
        RAISE EXCEPTION
            'refusing to run: attendance_tier2_runtime and attendance_tier2_cleanup must not be members of each other';
    END IF;

    IF NOT has_schema_privilege('attendance_tier2_runtime', 'public', 'USAGE') THEN
        RAISE EXCEPTION
            'refusing to run: attendance_tier2_runtime lacks USAGE on schema public (provision it with tools/setup_tier2_test_database.sql)';
    END IF;

    RAISE NOTICE 'preconditions satisfied; applying V002 to %, running as %',
        v_db, current_user;
END
$$;

-- --------------------------------------------------------------------------
-- 1. The migration itself, unmodified and checksummed.
--    SHA-256 of migrations/V002__security.sql's canonical text (UTF-8, BOM
--    stripped, CRLF/CR LF normalized to LF):
--      35bc00cd7a947a9eca31a7502d3a17e0ff55163f1c5222cf8fcb88a6328082d3
--    The block below is a byte-identical copy of that file (comments
--    included), spliced in mechanically; the commands run inside this open
--    transaction and are not committed until the final COMMIT.
-- --------------------------------------------------------------------------
-- --- begin verbatim migrations/V002__security.sql ---
-- Phase 5, P5.2 — Migration V002: identity and session tables.
--
-- Static SQL only, under the same approved migration contract as V001:
--   * no parameter substitution, no procedural branching;
--   * no CREATE DATABASE and no CREATE INDEX CONCURRENTLY (both are
--     prohibited: neither can run inside the runner's per-migration
--     transaction);
--   * executed through the Step 1.5b runner, one file per transaction,
--     on a positively verified target.
--
-- Payload is *identity metadata only*: no passwords in plaintext, no
-- tokens, no biometric columns, no embeddings and no attendance-event
-- tables. Nothing here changes the employee table or the authoritative
-- JSONL attendance journal.
--
-- Timestamp convention (unchanged from V001 / approved D2): every
-- timestamp is application-authored and inserted verbatim. Deliberately
-- **no database defaults** and no ``now()`` anywhere — a default would
-- silently substitute a time the application did not author.
--
-- Username normalization (documented contract, implemented in
-- ``src/user.py``): the *displayed* ``username`` is NFKC-normalized,
-- whitespace-trimmed with internal runs collapsed, and case-preserved;
-- ``username_key`` is the casefolded form of that same normalized text.
-- The UNIQUE constraint is on ``username_key`` only, so two accounts
-- cannot differ by case alone. Stored usernames are never rewritten by
-- the database.

CREATE TABLE app_user (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    username_key  TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,          -- complete Argon2id encoded hash (P5.3)
    role          TEXT NOT NULL CHECK (
        role IN ('Admin', 'HR Manager', 'Operator', 'Auditor')
    ),
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL,   -- application-authored; NO default
    updated_at    TIMESTAMPTZ NOT NULL    -- application-authored; NO default
);

-- Only the SHA-256 hex digest of an opaque session token is ever stored.
-- The original token exists solely in the client's HttpOnly cookie.
CREATE TABLE user_session (
    session_token_hash  CHAR(64) PRIMARY KEY,
    user_id             TEXT NOT NULL REFERENCES app_user(user_id),
    issued_at           TIMESTAMPTZ NOT NULL,
    expires_at          TIMESTAMPTZ NOT NULL,          -- idle deadline
    absolute_expires_at TIMESTAMPTZ NOT NULL,          -- hard cap
    revoked_at          TIMESTAMPTZ NULL,
    last_seen_at        TIMESTAMPTZ NOT NULL
);

-- Session lookup by user: "revoke all sessions for a user" and the
-- per-user session inventory both filter on user_id.
CREATE INDEX user_session_user_id_idx ON user_session (user_id);

-- Expiry/revocation maintenance: sweep idle and absolute deadlines
-- without scanning the whole table.
CREATE INDEX user_session_expires_at_idx ON user_session (expires_at);
-- --- end verbatim migrations/V002__security.sql ---

-- --------------------------------------------------------------------------
-- 2. Ownership assertion *before* granting: the executing role must own both
--    tables, otherwise GRANT would either fail or require grant option it
--    does not have. A mismatch is a refusal, not a silent skip.
-- --------------------------------------------------------------------------
DO $$
DECLARE
    v_bad text;
BEGIN
    SELECT string_agg(c.relname, ', ' ORDER BY c.relname)
      INTO v_bad
      FROM pg_class c
     WHERE c.oid IN ('public.app_user'::regclass, 'public.user_session'::regclass)
       AND pg_get_userbyid(c.relowner) <> current_user;
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION
            'refusing to grant: role % does not own %', current_user, v_bad;
    END IF;
END
$$;

-- --------------------------------------------------------------------------
-- 3. The version-2 ledger row, in the SAME transaction as the DDL.
--    Type and instant semantics: TIMESTAMPTZ not null, exactly as the runner
--    writes it. migrate() captures one client-side datetime.now(UTC) at run
--    start; now() is this transaction's start timestamptz, the SQL-side
--    analogue ("when the migration was applied"). Nothing in the system ever
--    interprets applied_at (reconciliation uses version + checksum only), and
--    V001's provisioning used now() as well.
-- --------------------------------------------------------------------------
INSERT INTO schema_version (version, checksum, applied_at)
VALUES (
    2,
    '35bc00cd7a947a9eca31a7502d3a17e0ff55163f1c5222cf8fcb88a6328082d3',
    now()
);

-- --------------------------------------------------------------------------
-- 4. Grants, minimal and object-scoped, in the same transaction.
--
--    attendance_tier2_runtime — the application's own SQL
--    (src/user_repository.py, src/session_repository.py) only reads, inserts
--    and updates: user creation, role/active/password-hash updates, session
--    touch/revoke. DELETE is deliberately withheld.
--
--    attendance_tier2_cleanup — test-only teardown of synthetic T2-UC-* rows
--    and their sessions. DELETE on exactly those two tables; no read or write
--    privilege, no other table, no DDL. USAGE on schema public is granted
--    only if the role does not already hold it: PostgreSQL grants USAGE on
--    schema public to PUBLIC by default, so the grant is usually a no-op,
--    and GRANT requires grant option even when the privilege already
--    exists. Skipping it keeps the owner from needing grant option it
--    does not otherwise use.
-- --------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON public.app_user TO attendance_tier2_runtime;
GRANT SELECT, INSERT, UPDATE ON public.user_session TO attendance_tier2_runtime;

-- The cleanup role needs the user_id column to drive its DELETEs
-- (WHERE user_id = ANY(...)). Grant column-level SELECT on user_id only —
-- no table-level read, no other column, no INSERT/UPDATE.
GRANT SELECT (user_id) ON public.app_user TO attendance_tier2_cleanup;
GRANT SELECT (user_id) ON public.user_session TO attendance_tier2_cleanup;

GRANT DELETE ON public.app_user TO attendance_tier2_cleanup;
GRANT DELETE ON public.user_session TO attendance_tier2_cleanup;

DO $$
BEGIN
    IF NOT has_schema_privilege('attendance_tier2_cleanup', 'public', 'USAGE') THEN
        IF NOT has_schema_privilege(
                   current_user, 'public', 'USAGE WITH GRANT OPTION'
               ) THEN
            RAISE EXCEPTION
                'refusing to grant: attendance_tier2_cleanup lacks USAGE on schema public and role % has no grant option to grant it',
                current_user;
        END IF;
        EXECUTE 'GRANT USAGE ON SCHEMA public TO attendance_tier2_cleanup';
        RAISE NOTICE 'granted USAGE on schema public to attendance_tier2_cleanup';
    ELSE
        RAISE NOTICE
            'attendance_tier2_cleanup already holds USAGE on schema public; schema grant skipped';
    END IF;
END
$$;

-- --------------------------------------------------------------------------
-- 5. Post-apply verification. Any deviation raises and rolls the whole
--    transaction back: an applied-but-wrong schema is never left behind.
--    Contract checked: tables, every column (type, nullability, default),
--    primary keys, the username_key UNIQUE constraint, the role CHECK, the
--    session foreign key, both indexes, both ledger rows, the V001 employee
--    schema, the Tier-2 guard marker and the exact privilege sets.
-- --------------------------------------------------------------------------
DO $$
DECLARE
    v_count      integer;
    v_bad        text;
    v_len        integer;
    v_checksum   text;
BEGIN
    -- 5a. Both tables exist.
    IF to_regclass('public.app_user') IS NULL
       OR to_regclass('public.user_session') IS NULL THEN
        RAISE EXCEPTION 'app_user and user_session must both exist after V002';
    END IF;

    -- 5b. Column counts.
    SELECT count(*) INTO v_count
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'app_user';
    IF v_count <> 8 THEN
        RAISE EXCEPTION 'app_user must have exactly 8 columns, found %', v_count;
    END IF;

    SELECT count(*) INTO v_count
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'user_session';
    IF v_count <> 7 THEN
        RAISE EXCEPTION 'user_session must have exactly 7 columns, found %', v_count;
    END IF;

    -- 5c. Every column's name, type, nullability and default.
    SELECT string_agg(e.tbl || '.' || e.col, ', ' ORDER BY e.tbl, e.col)
      INTO v_bad
      FROM (VALUES
            ('app_user', 'user_id', 'text', 'NO', NULL::text),
            ('app_user', 'username', 'text', 'NO', NULL::text),
            ('app_user', 'username_key', 'text', 'NO', NULL::text),
            ('app_user', 'password_hash', 'text', 'NO', NULL::text),
            ('app_user', 'role', 'text', 'NO', NULL::text),
            ('app_user', 'is_active', 'boolean', 'NO', 'true'),
            ('app_user', 'created_at', 'timestamp with time zone', 'NO', NULL::text),
            ('app_user', 'updated_at', 'timestamp with time zone', 'NO', NULL::text),
            ('user_session', 'session_token_hash', 'character', 'NO', NULL::text),
            ('user_session', 'user_id', 'text', 'NO', NULL::text),
            ('user_session', 'issued_at', 'timestamp with time zone', 'NO', NULL::text),
            ('user_session', 'expires_at', 'timestamp with time zone', 'NO', NULL::text),
            ('user_session', 'absolute_expires_at', 'timestamp with time zone', 'NO', NULL::text),
            ('user_session', 'revoked_at', 'timestamp with time zone', 'YES', NULL::text),
            ('user_session', 'last_seen_at', 'timestamp with time zone', 'NO', NULL::text)
           ) AS e(tbl, col, typ, nul, dflt)
     WHERE NOT EXISTS (
         SELECT 1
           FROM information_schema.columns c
          WHERE c.table_schema = 'public'
            AND c.table_name = e.tbl
            AND c.column_name = e.col
            AND c.data_type = e.typ
            AND c.is_nullable = e.nul
            AND c.column_default IS NOT DISTINCT FROM e.dflt
     );
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'column contract deviation: %', v_bad;
    END IF;

    -- 5d. session_token_hash is character(64), not an unbounded text.
    SELECT character_maximum_length INTO v_len
      FROM information_schema.columns
     WHERE table_schema = 'public'
       AND table_name = 'user_session'
       AND column_name = 'session_token_hash';
    IF v_len IS DISTINCT FROM 64 THEN
        RAISE EXCEPTION
            'session_token_hash must be character(64), found length %', v_len;
    END IF;

    -- 5e. Primary key of app_user is exactly (user_id).
    SELECT count(*) INTO v_count
      FROM pg_index i
      JOIN pg_class c     ON c.oid = i.indrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public'
       AND c.relname = 'app_user'
       AND i.indisprimary
       AND i.indisunique
       AND i.indnatts = 1
       AND (SELECT a.attname
              FROM pg_attribute a
             WHERE a.attrelid = c.oid
               AND a.attnum = i.indkey[0]) = 'user_id';
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'app_user primary key must be exactly (user_id)';
    END IF;

    -- 5f. Primary key of user_session is exactly (session_token_hash).
    SELECT count(*) INTO v_count
      FROM pg_index i
      JOIN pg_class c     ON c.oid = i.indrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public'
       AND c.relname = 'user_session'
       AND i.indisprimary
       AND i.indisunique
       AND i.indnatts = 1
       AND (SELECT a.attname
              FROM pg_attribute a
             WHERE a.attrelid = c.oid
               AND a.attnum = i.indkey[0]) = 'session_token_hash';
    IF v_count <> 1 THEN
        RAISE EXCEPTION
            'user_session primary key must be exactly (session_token_hash)';
    END IF;

    -- 5g. UNIQUE constraint on app_user(username_key) — case-insensitive
    --     uniqueness depends on it.
    SELECT count(*) INTO v_count
      FROM pg_constraint con
     WHERE con.conrelid = 'public.app_user'::regclass
       AND con.contype = 'u'
       AND con.conkey = ARRAY[
               (SELECT a.attnum
                  FROM pg_attribute a
                 WHERE a.attrelid = con.conrelid
                   AND a.attname = 'username_key')
           ];
    IF v_count <> 1 THEN
        RAISE EXCEPTION
            'app_user must have a UNIQUE constraint on (username_key)';
    END IF;

    -- 5h. Exactly one CHECK constraint, restricting role to the four
    --     documented values.
    SELECT count(*) INTO v_count
      FROM pg_constraint
     WHERE conrelid = 'public.app_user'::regclass
       AND contype = 'c';
    IF v_count <> 1 THEN
        RAISE EXCEPTION
            'app_user must have exactly one CHECK constraint, found %', v_count;
    END IF;

    SELECT count(*) INTO v_count
      FROM pg_constraint
     WHERE conrelid = 'public.app_user'::regclass
       AND contype = 'c'
       AND pg_get_constraintdef(oid) ILIKE '%role%'
       AND pg_get_constraintdef(oid) LIKE '%Admin%'
       AND pg_get_constraintdef(oid) LIKE '%HR Manager%'
       AND pg_get_constraintdef(oid) LIKE '%Operator%'
       AND pg_get_constraintdef(oid) LIKE '%Auditor%';
    IF v_count <> 1 THEN
        RAISE EXCEPTION
            'app_user role CHECK must permit exactly Admin, HR Manager, Operator, Auditor';
    END IF;

    -- 5i. user_session.user_id references app_user(user_id).
    SELECT count(*) INTO v_count
      FROM pg_constraint con
     WHERE con.conrelid = 'public.user_session'::regclass
       AND con.contype = 'f'
       AND con.confrelid = 'public.app_user'::regclass
       AND con.conkey = ARRAY[
               (SELECT a.attnum
                  FROM pg_attribute a
                 WHERE a.attrelid = con.conrelid
                   AND a.attname = 'user_id')
           ]
       AND con.confkey = ARRAY[
               (SELECT a.attnum
                  FROM pg_attribute a
                 WHERE a.attrelid = con.confrelid
                   AND a.attname = 'user_id')
           ];
    IF v_count <> 1 THEN
        RAISE EXCEPTION
            'user_session.user_id must have a foreign key to app_user(user_id)';
    END IF;

    -- 5j. Both V002 indexes exist, valid, plain, single-column, right column.
    SELECT string_agg(e.idx, ', ' ORDER BY e.idx) INTO v_bad
      FROM (VALUES
            ('user_session_user_id_idx', 'user_id'),
            ('user_session_expires_at_idx', 'expires_at')
           ) AS e(idx, col)
     WHERE NOT EXISTS (
         SELECT 1
           FROM pg_index i
           JOIN pg_class ic    ON ic.oid = i.indexrelid
           JOIN pg_class tc    ON tc.oid = i.indrelid
           JOIN pg_namespace n ON n.oid = tc.relnamespace
          WHERE n.nspname = 'public'
            AND tc.relname = 'user_session'
            AND ic.relname = e.idx
            AND i.indisvalid
            AND NOT i.indisunique
            AND i.indpred IS NULL
            AND i.indexprs IS NULL
            AND i.indnatts = 1
            AND (SELECT a.attname
                   FROM pg_attribute a
                  WHERE a.attrelid = tc.oid
                    AND a.attnum = i.indkey[0]) = e.col
     );
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'V002 indexes missing or wrong: %', v_bad;
    END IF;

    -- 5k. Ledger: exactly versions 1 and 2, both verified checksums.
    SELECT count(*) INTO v_count FROM schema_version;
    IF v_count <> 2 THEN
        RAISE EXCEPTION 'schema_version must contain exactly 2 rows, found %', v_count;
    END IF;

    SELECT checksum INTO v_checksum FROM schema_version WHERE version = 1;
    IF v_checksum IS DISTINCT FROM
       '9e6ac6b5d1e0fd016a99c007ec5b91070dd480853d54d37cdab5b069dd2deac9' THEN
        RAISE EXCEPTION 'the V001 ledger row changed; refusing to claim a clean apply';
    END IF;

    SELECT checksum INTO v_checksum FROM schema_version WHERE version = 2;
    IF v_checksum IS DISTINCT FROM
       '35bc00cd7a947a9eca31a7502d3a17e0ff55163f1c5222cf8fcb88a6328082d3' THEN
        RAISE EXCEPTION 'the version-2 ledger checksum is not the verified V002 checksum';
    END IF;

    -- 5l. V001 employee schema still exactly as provisioned.
    SELECT count(*) INTO v_count
      FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = 'employee';
    IF v_count <> 4 THEN
        RAISE EXCEPTION 'employee must still have exactly 4 columns, found %', v_count;
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

    SELECT count(*) INTO v_count
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
               AND a.attnum = i.indkey[0]) = 'employee_id';
    IF v_count <> 1 THEN
        RAISE EXCEPTION 'employee primary key must be exactly (employee_id)';
    END IF;

    -- 5m. Tier-2 guard marker unchanged.
    IF NOT EXISTS (
        SELECT 1 FROM _phase3_test_guard
         WHERE marker = 'VISUAL_ATTENDANCE_PHASE3_TEST_ONLY'
           AND schema_version = 1
    ) THEN
        RAISE EXCEPTION 'the Tier-2 guard marker changed during the apply';
    END IF;

    -- 5n. Runtime privileges: SELECT/INSERT/UPDATE present, DELETE absent,
    --     column-level SELECT absent (the runtime role reads via the table).
    SELECT string_agg(t.name || '/' || p.priv, ', ')
      INTO v_bad
      FROM unnest(ARRAY['app_user', 'user_session']) AS t(name)
      CROSS JOIN unnest(ARRAY['SELECT', 'INSERT', 'UPDATE']) AS p(priv)
     WHERE NOT has_table_privilege(
               'attendance_tier2_runtime', 'public.' || t.name, p.priv
           );
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'required runtime privileges missing: %', v_bad;
    END IF;

    SELECT string_agg(t.name, ', ')
      INTO v_bad
      FROM unnest(ARRAY['app_user', 'user_session']) AS t(name)
     WHERE has_table_privilege(
               'attendance_tier2_runtime', 'public.' || t.name, 'DELETE'
           );
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION
            'the runtime role must not hold DELETE on % (synthetic-row cleanup uses the separate cleanup role)',
            v_bad;
    END IF;

    -- 5n-bis. The runtime role must hold no column-level grant at all: it
    --     reads through its table-level SELECT, so a column-level record would
    --     be unintended (and a sign the grant set has drifted).
    --     information_schema.column_privileges shows each row to its grantor,
    --     so the executing owner sees the grants it just made. ``grantee`` in
    --     that view is already a role NAME, never an OID, so it must be
    --     compared as a name: passing it to pg_get_userbyid() is the same class
    --     of type error the earlier revision of this script had, and would
    --     either fail the apply or silently compare nothing.
    SELECT string_agg(
               'public.' || c.table_name::text || '.' || c.column_name::text ||
               '/' || c.privilege_type::text, ', '
           ORDER BY c.table_name, c.column_name, c.privilege_type)
      INTO v_bad
      FROM information_schema.column_privileges c
     WHERE c.table_schema = 'public'
       AND c.table_name IN ('app_user', 'user_session')
       AND c.grantee = 'attendance_tier2_runtime';
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION
            'the runtime role must not hold column-level grants; found %',
            v_bad;
    END IF;

    -- 5o. Cleanup role, effective privileges (not only the grant records this
    --     script wrote): DELETE on both tables and no table-level INSERT or
    --     UPDATE. Every check goes through PostgreSQL's own privilege
    --     functions, so a privilege inherited through role membership is
    --     included. Table-level SELECT is deliberately not tested with
    --     has_table_privilege() here: that answers the table-level ACL only,
    --     while the effective read surface is asserted per column in 5p, where
    --     a table-level grant makes every column readable.
    SELECT string_agg(t.name || '/DELETE', ', ' ORDER BY t.name)
      INTO v_bad
      FROM unnest(ARRAY['app_user', 'user_session']) AS t(name)
     WHERE NOT has_table_privilege(
               'attendance_tier2_cleanup', 'public.' || t.name, 'DELETE'
           );
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION 'the cleanup role lacks DELETE on %', v_bad;
    END IF;

    SELECT string_agg(t.name || '/' || p.priv, ', ' ORDER BY t.name, p.priv)
      INTO v_bad
      FROM unnest(ARRAY['app_user', 'user_session']) AS t(name)
     CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE']) AS p(priv)
     WHERE has_table_privilege(
               'attendance_tier2_cleanup', 'public.' || t.name, p.priv
           );
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION
            'the cleanup role must hold no table-level write privilege; found %',
            v_bad;
    END IF;

    -- 5p. Cleanup role: effective column-level SELECT on user_id only, in both
    --     tables. has_column_privilege() succeeds either when the privilege is
    --     held for the whole table or when it is granted for that column, so
    --     this single per-column sweep rejects a table-level SELECT grant, a
    --     grant on any other column, and any SELECT inherited from another
    --     role. The column list comes from the catalog, so columns added by a
    --     later migration are covered without editing this block.
    SELECT string_agg(c.table_name::text || '.' || c.column_name::text, ', '
                      ORDER BY c.table_name, c.column_name)
      INTO v_bad
      FROM information_schema.columns c
     WHERE c.table_schema = 'public'
       AND c.table_name IN ('app_user', 'user_session')
       AND has_column_privilege(
               'attendance_tier2_cleanup'::name,
               ('public.' || c.table_name)::text,
               c.column_name::text,
               'SELECT'
           )
       AND NOT (c.table_name = 'app_user' AND c.column_name = 'user_id')
       AND NOT (c.table_name = 'user_session' AND c.column_name = 'user_id');
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION
            'the cleanup role must only be able to read user_id, but can read %',
            v_bad;
    END IF;

    SELECT string_agg(t.name, ', ' ORDER BY t.name)
      INTO v_bad
      FROM unnest(ARRAY['app_user', 'user_session']) AS t(name)
     WHERE NOT has_column_privilege(
               'attendance_tier2_cleanup', 'public.' || t.name, 'user_id', 'SELECT'
           );
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION
            'the cleanup role lacks column-level SELECT on user_id in %', v_bad;
    END IF;

    -- The teardown role has no reason to hold column-level writes either.
    SELECT string_agg(
               c.table_name::text || '.' || c.column_name::text || '/' || p.priv,
               ', ' ORDER BY c.table_name, c.column_name, p.priv)
      INTO v_bad
      FROM information_schema.columns c
      CROSS JOIN unnest(ARRAY['INSERT', 'UPDATE']) AS p(priv)
     WHERE c.table_schema = 'public'
       AND c.table_name IN ('app_user', 'user_session')
       AND has_column_privilege(
               'attendance_tier2_cleanup'::name,
               ('public.' || c.table_name)::text,
               c.column_name::text,
               p.priv
           );
    IF v_bad IS NOT NULL THEN
        RAISE EXCEPTION
            'the cleanup role must hold no column-level write privilege; found %',
            v_bad;
    END IF;

    IF NOT has_schema_privilege('attendance_tier2_runtime', 'public', 'USAGE') THEN
        RAISE EXCEPTION 'attendance_tier2_runtime lacks USAGE on schema public';
    END IF;
    IF NOT has_schema_privilege('attendance_tier2_cleanup', 'public', 'USAGE') THEN
        RAISE EXCEPTION 'attendance_tier2_cleanup lacks USAGE on schema public';
    END IF;

    RAISE NOTICE
        'V002 applied and verified: app_user, user_session, ledger rows 1-2, runtime SELECT/INSERT/UPDATE without DELETE, cleanup DELETE plus column-level SELECT on user_id only';
END
$$;

COMMIT;
