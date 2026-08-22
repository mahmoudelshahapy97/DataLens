-- Session scoping, external identity, and self-service password reset.
--
-- Three gaps this closes.
--
-- `must_change` was advisory. The API issued a full 72-hour session and only the
-- browser honoured the flag, so a temporary password was in practice a permanent
-- credential for anything that spoke HTTP. `sessions.scope` makes the restriction
-- something the server enforces: a session issued for a must-change account can
-- reach the password endpoint and nothing else.
--
-- `users` assumed a password existed. An OIDC account has none, and a row with an
-- empty `password_hash` would silently accept `verify_password('', '')` if that
-- function ever grew laxer. `auth_provider` states the intent instead of inferring
-- it from an empty column.
--
-- There was no way to recover an account. `password_resets` holds hashed,
-- single-use, short-lived tokens -- the same treatment sessions get, for the same
-- reason: a dump of this table must not be replayable.

-- ----------------------------------------------------------------------
-- Session scope
-- ----------------------------------------------------------------------

ALTER TABLE vanna_app.sessions
    ADD COLUMN IF NOT EXISTS scope text NOT NULL DEFAULT 'full';

ALTER TABLE vanna_app.sessions
    DROP CONSTRAINT IF EXISTS sessions_scope_check;

ALTER TABLE vanna_app.sessions
    ADD CONSTRAINT sessions_scope_check
    CHECK (scope IN ('full', 'password_change_only'));

-- ----------------------------------------------------------------------
-- Where an identity comes from
-- ----------------------------------------------------------------------

ALTER TABLE vanna_app.users
    ADD COLUMN IF NOT EXISTS auth_provider text NOT NULL DEFAULT 'password';

ALTER TABLE vanna_app.users
    ADD COLUMN IF NOT EXISTS external_id text;

ALTER TABLE vanna_app.users
    ADD COLUMN IF NOT EXISTS password_changed_at timestamptz;

-- Backfill: everything that exists today was created with a password.
UPDATE vanna_app.users
   SET password_changed_at = created_at
 WHERE password_changed_at IS NULL;

CREATE INDEX IF NOT EXISTS users_external_idx
    ON vanna_app.users (auth_provider, external_id);

-- ----------------------------------------------------------------------
-- Password reset
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS vanna_app.password_resets (
    token_hash text        PRIMARY KEY,
    email      text        NOT NULL REFERENCES vanna_app.users(email) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    -- Single use. Set on redemption rather than deleting the row, so a second
    -- attempt with the same link can be distinguished from an unknown one in the
    -- logs -- without telling the caller which it was.
    used_at    timestamptz,
    requested_ip text      NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS password_resets_email_idx
    ON vanna_app.password_resets (email);
CREATE INDEX IF NOT EXISTS password_resets_expiry_idx
    ON vanna_app.password_resets (expires_at);

-- ----------------------------------------------------------------------
-- Datasource credentials at rest
-- ----------------------------------------------------------------------

-- The application seals `database_url` with Fernet from this release on. Existing
-- rows stay readable (the reader passes through anything without the `enc:v1:`
-- marker) and are re-written sealed the next time the workspace is saved.
--
-- Deliberately not re-encrypted here: this migration has no access to
-- VANNA_SECRET_KEY, and a migration that silently no-ops when a key is absent is
-- worse than one that never claimed to do the work. `vanna-app seal-secrets`
-- performs the backfill with the key in hand.
COMMENT ON COLUMN vanna_app.tenants.database_url IS
    'Connection URL. Sealed with Fernet under VANNA_SECRET_KEY; values prefixed '
    'enc:v1: are ciphertext. Legacy plaintext rows are still readable.';
