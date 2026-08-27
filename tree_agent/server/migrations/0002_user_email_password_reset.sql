-- Account recovery data.  Existing installations may already have users, so
-- email starts nullable and the desktop asks those users to add and verify it.
ALTER TABLE users ADD COLUMN email TEXT COLLATE NOCASE;
ALTER TABLE users ADD COLUMN email_verified_at_ms INTEGER;
CREATE UNIQUE INDEX uq_users_email ON users(email) WHERE email IS NOT NULL;

-- Codes are deliberately never stored in the clear.  `code_salt` is unique
-- per request, so a database backup cannot cheaply enumerate six-digit codes.
CREATE TABLE password_reset_codes (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  purpose TEXT NOT NULL CHECK (purpose IN ('password_reset', 'email_verify')),
  email TEXT NOT NULL COLLATE NOCASE,
  code_salt TEXT NOT NULL,
  code_hash TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
  expires_at_ms INTEGER NOT NULL,
  consumed_at_ms INTEGER,
  created_at_ms INTEGER NOT NULL
);
CREATE INDEX idx_password_reset_codes_lookup
  ON password_reset_codes(email, purpose, expires_at_ms)
  WHERE consumed_at_ms IS NULL;
CREATE INDEX idx_password_reset_codes_user ON password_reset_codes(user_id, purpose);
