-- Administrator-managed SMTP delivery settings.  The password is protected
-- outside SQLite (DPAPI on Windows, a host-local key elsewhere), so the
-- database never contains a usable SMTP secret in clear text.
CREATE TABLE mail_settings (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  host TEXT NOT NULL,
  port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
  from_address TEXT NOT NULL,
  encryption TEXT NOT NULL CHECK (encryption IN ('none', 'starttls', 'ssl')),
  username TEXT NOT NULL DEFAULT '',
  password_protected TEXT,
  updated_at_ms INTEGER NOT NULL
);
