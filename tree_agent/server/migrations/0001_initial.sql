-- Tree Agent shared workspace, initial schema.
--
-- CHECK constraints here cover fixed enumerations only. Business rules
-- (permissions, tree legality, name uniqueness across a move) are enforced by
-- the service layer inside the same transaction as the write.

CREATE TABLE schema_migrations (
  version INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  applied_at_ms INTEGER NOT NULL
);

CREATE TABLE users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL COLLATE NOCASE UNIQUE,
  password_hash TEXT NOT NULL,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('admin','member','viewer')),
  is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL,
  deleted_at_ms INTEGER
);

CREATE TABLE auth_sessions (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  token_hash TEXT NOT NULL UNIQUE,
  expires_at_ms INTEGER NOT NULL,
  last_seen_at_ms INTEGER NOT NULL,
  revoked_at_ms INTEGER,
  created_at_ms INTEGER NOT NULL
);
CREATE INDEX idx_auth_sessions_active
  ON auth_sessions(token_hash, expires_at_ms) WHERE revoked_at_ms IS NULL;
CREATE INDEX idx_auth_sessions_user ON auth_sessions(user_id);

CREATE TABLE idempotency_keys (
  user_id TEXT NOT NULL REFERENCES users(id),
  request_key TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  status_code INTEGER NOT NULL,
  response_json TEXT NOT NULL,
  created_at_ms INTEGER NOT NULL,
  expires_at_ms INTEGER NOT NULL,
  PRIMARY KEY (user_id, request_key)
);
CREATE INDEX idx_idempotency_expiry ON idempotency_keys(expires_at_ms);

CREATE TABLE projects (
  id TEXT PRIMARY KEY,
  parent_id TEXT REFERENCES projects(id),
  name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 200),
  sort_key TEXT NOT NULL,
  cwd TEXT,
  model TEXT,
  sandbox TEXT,
  claude_permission TEXT,
  prompt TEXT,
  is_expanded INTEGER NOT NULL DEFAULT 1 CHECK (is_expanded IN (0,1)),
  revision INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL REFERENCES users(id),
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL,
  deleted_at_ms INTEGER,
  deleted_by TEXT REFERENCES users(id)
);
CREATE UNIQUE INDEX uq_projects_live_sibling_name
  ON projects(COALESCE(parent_id, ''), name COLLATE NOCASE)
  WHERE deleted_at_ms IS NULL;
CREATE INDEX idx_projects_live_parent_sort
  ON projects(parent_id, sort_key) WHERE deleted_at_ms IS NULL;

CREATE TABLE project_memberships (
  project_id TEXT NOT NULL REFERENCES projects(id),
  user_id TEXT NOT NULL REFERENCES users(id),
  permission TEXT NOT NULL CHECK (permission IN ('owner','editor','viewer')),
  granted_by TEXT NOT NULL REFERENCES users(id),
  created_at_ms INTEGER NOT NULL,
  PRIMARY KEY (project_id, user_id)
);
CREATE INDEX idx_memberships_user ON project_memberships(user_id, project_id);

CREATE TABLE conversations (
  id TEXT PRIMARY KEY,
  project_id TEXT NOT NULL REFERENCES projects(id),
  name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 200),
  sort_key TEXT NOT NULL,
  agent_id TEXT NOT NULL DEFAULT 'codex' CHECK (agent_id IN ('codex','claude')),
  model TEXT,
  codex_thread_id TEXT,
  claude_session_id TEXT,
  forked_from_conversation_id TEXT REFERENCES conversations(id),
  forked_from_external_session_id TEXT,
  revision INTEGER NOT NULL DEFAULT 1,
  created_by TEXT NOT NULL REFERENCES users(id),
  created_at_ms INTEGER NOT NULL,
  updated_at_ms INTEGER NOT NULL,
  deleted_at_ms INTEGER,
  deleted_by TEXT REFERENCES users(id)
);
CREATE UNIQUE INDEX uq_conversations_live_project_name
  ON conversations(project_id, name COLLATE NOCASE) WHERE deleted_at_ms IS NULL;
CREATE INDEX idx_conversations_live_project_sort
  ON conversations(project_id, sort_key) WHERE deleted_at_ms IS NULL;

CREATE TABLE messages (
  id TEXT PRIMARY KEY,
  conversation_id TEXT NOT NULL REFERENCES conversations(id),
  parent_message_id TEXT REFERENCES messages(id),
  sequence_no INTEGER NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('user','agent','reasoning','tool','error','notice','meta')),
  content TEXT NOT NULL DEFAULT '',
  content_format TEXT NOT NULL DEFAULT 'plain' CHECK (content_format IN ('plain','markdown','json')),
  agent_id TEXT,
  model TEXT,
  external_event_id TEXT,
  metadata_json TEXT NOT NULL DEFAULT '{}',
  created_by TEXT REFERENCES users(id),
  created_at_ms INTEGER NOT NULL,
  completed_at_ms INTEGER,
  deleted_at_ms INTEGER,
  UNIQUE (conversation_id, sequence_no),
  UNIQUE (conversation_id, external_event_id)
);
CREATE INDEX idx_messages_live_conversation_sequence
  ON messages(conversation_id, sequence_no) WHERE deleted_at_ms IS NULL;

CREATE TABLE tool_calls (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL REFERENCES messages(id),
  call_index INTEGER NOT NULL,
  tool_name TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','running','completed','failed','cancelled')),
  input_json TEXT NOT NULL DEFAULT '{}',
  output_text TEXT NOT NULL DEFAULT '',
  error_text TEXT,
  started_at_ms INTEGER,
  completed_at_ms INTEGER,
  UNIQUE(message_id, call_index)
);
CREATE INDEX idx_tool_calls_message ON tool_calls(message_id, call_index);

CREATE TABLE attachments (
  id TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL,
  file_name TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  byte_size INTEGER NOT NULL CHECK (byte_size > 0 AND byte_size <= 20971520),
  chunk_count INTEGER NOT NULL CHECK (chunk_count > 0),
  created_by TEXT NOT NULL REFERENCES users(id),
  created_at_ms INTEGER NOT NULL,
  deleted_at_ms INTEGER,
  UNIQUE(sha256, byte_size)
);
CREATE TABLE attachment_chunks (
  attachment_id TEXT NOT NULL REFERENCES attachments(id),
  chunk_no INTEGER NOT NULL,
  bytes BLOB NOT NULL,
  PRIMARY KEY (attachment_id, chunk_no)
) WITHOUT ROWID;
CREATE TABLE message_attachments (
  message_id TEXT NOT NULL REFERENCES messages(id),
  attachment_id TEXT NOT NULL REFERENCES attachments(id),
  display_order INTEGER NOT NULL,
  PRIMARY KEY (message_id, attachment_id),
  UNIQUE(message_id, display_order)
);
CREATE INDEX idx_message_attachments_attachment ON message_attachments(attachment_id);

CREATE TABLE attachment_uploads (
  id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  target_message_id TEXT REFERENCES messages(id),
  file_name TEXT NOT NULL,
  mime_type TEXT NOT NULL,
  expected_byte_size INTEGER NOT NULL CHECK (expected_byte_size > 0 AND expected_byte_size <= 20971520),
  expected_sha256 TEXT,
  status TEXT NOT NULL CHECK (status IN ('uploading','committed','expired','failed')),
  received_byte_size INTEGER NOT NULL DEFAULT 0,
  created_at_ms INTEGER NOT NULL,
  expires_at_ms INTEGER NOT NULL,
  committed_attachment_id TEXT REFERENCES attachments(id)
);
CREATE TABLE attachment_upload_chunks (
  upload_id TEXT NOT NULL REFERENCES attachment_uploads(id),
  chunk_no INTEGER NOT NULL,
  bytes BLOB NOT NULL,
  PRIMARY KEY (upload_id, chunk_no)
) WITHOUT ROWID;
CREATE INDEX idx_attachment_uploads_expiry ON attachment_uploads(status, expires_at_ms);

CREATE TABLE migration_reports (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  source_path TEXT NOT NULL,
  started_at_ms INTEGER NOT NULL,
  completed_at_ms INTEGER,
  status TEXT NOT NULL CHECK (status IN ('running','completed','failed')),
  summary_json TEXT NOT NULL DEFAULT '{}',
  issues_json TEXT NOT NULL DEFAULT '[]',
  created_by TEXT NOT NULL REFERENCES users(id)
);

-- The three search indexes are maintained by the service layer, in the same
-- transaction as the row they describe. Triggers cannot do it: moving a
-- conversation to another project has to reindex that conversation and every
-- live message under it, which the trigger has no way to see.
CREATE VIRTUAL TABLE project_fts USING fts5(
  project_id UNINDEXED, name, prompt, tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE conversation_fts USING fts5(
  conversation_id UNINDEXED, project_id UNINDEXED, name,
  tokenize='unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE message_fts USING fts5(
  message_id UNINDEXED, conversation_id UNINDEXED, project_id UNINDEXED, content,
  tokenize='unicode61 remove_diacritics 2'
);
