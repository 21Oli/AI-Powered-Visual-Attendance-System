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
