# Tree Agent：多人共用 SQLite 儲存實作規格

## 1. 目的與已確認決策

將目前以單一 `workspace.json` 與本機附件路徑保存的 Tree Agent，改為由**單一服務主機**集中讀寫 SQLite 的儲存架構。桌面程式不可直接開啟 `.db` 檔，也不可將資料庫放在 NAS／共享磁碟後供多台電腦直接存取。

已確認需求：

- 多人可使用同一個工作區資料庫。
- 必須登入，並有使用者、角色與專案存取權限。
- 專案可無限層級巢狀；對話只能直接隸屬一個專案。
- 保留所有訊息、Agent／模型、工具呼叫、工具輸出、執行狀態與附件。
- 刪除採軟刪除，保留復原能力。
- 附件內容直接存 SQLite；單一附件最大 20 MiB，預估總資料量 100 GiB。
- 必須可全文搜尋專案、對話與訊息；不需即時推播。

## 2. 範圍與非目標

本次包含：資料模型、登入與授權邊界、SQLite 設定、API 儲存層、從舊 JSON 匯入、全文搜尋、備份及測試。

本次不包含：多人即時同步 UI、雲端多區部署、將 Codex／Claude 原生 session 移到另一台主機後仍可 resume。舊的 `thread_id`／`claude_session_id` 是 runner 的外部識別，匯入時保留作歷史資料，但是否可繼續執行應由服務主機實際確認。

## 3. 目標架構

```
Tree Agent 桌面客戶端 (每位使用者)
        │ HTTPS / localhost reverse proxy
        ▼
單一 Tree Agent 服務主機
  ├─ Authentication / authorization
  ├─ API / application service / write queue
  ├─ Codex、Claude runner（如服務端執行）
  └─ SQLite（主機本機 SSD）
       └─ 附件 BLOB 與全文索引
```

資料庫路徑必須在服務主機的本機磁碟，例如 `D:\TreeAgentData\tree-agent.db`。客戶端只保存服務網址及受保護的登入憑證，不保存完整工作區副本。

SQLite 允許多讀者但同時只能有一位寫者。因此服務必須在程序內將所有寫入排入單一 FIFO writer queue；讀取連線可獨立使用。這避免大型附件寫入與串流訊息更新彼此頻繁得到 `database is locked`。

## 4. 權限模型

### 4.1 角色

| 角色 | 權限 |
| --- | --- |
| `admin` | 管理使用者、所有專案、刪除／復原、備份與系統設定 |
| `member` | 僅能讀寫獲授權專案；可建立子專案、對話與訊息 |
| `viewer` | 僅讀取獲授權專案及下載附件 |

專案權限由 `project_memberships` 指定，子專案自最近已設定權限的祖先專案繼承。`admin` 不受 membership 限制。第一次啟動必須以一次性初始化密碼建立首位 `admin`；不可預設帳密。

每個 API 請求都要由 authenticated user 進行 `can_read_project`、`can_write_project` 或 `is_admin` 檢查；不得只靠 UI 隱藏按鈕。

精確繼承規則：對某使用者，自目標 project 向根逐層尋找**該使用者**的 membership，第一個找到的 permission 即為有效權限；找不到即無權限。子專案為同一使用者新增 membership 可覆寫父層（例如父層 viewer、子層 editor）。`owner` 與 `editor` 都可寫入，僅 owner／admin 可改該 project 的 memberships；新建 project 的建立者自動取得 owner。project 上存在其他使用者的 membership，不影響此使用者的繼承結果。

## 5. SQLite 設定與資料完整性

服務啟動後，每一條連線都必須執行：

```sql
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;
PRAGMA busy_timeout = 10000;
PRAGMA temp_store = MEMORY;
PRAGMA wal_autocheckpoint = 1000;
```

- 使用 SQLite 3.35+，並確認編譯支援 FTS5 與 `RETURNING`。
- 所有時間以 UTC epoch milliseconds 的 `INTEGER` 保存；API 回傳 ISO-8601 UTC。
- 所有 ID 使用 UUIDv4 字串；不重用已刪除資料的 ID。
- 結構、權限、訊息、附件 metadata 的更新都必須用交易。寫入時以 `BEGIN IMMEDIATE` 取得寫者權利，失敗由 writer queue 重試，不讓客戶端自行重送而製造重複資料。
- 使用 optimistic concurrency：可被多人修改的 project、conversation 帶 `revision`。修改／移動／刪除請求必須帶最後讀到的 revision；不符時回 `409 Conflict` 並回傳目前版本。
- 任何 SQL 一律使用參數繫結，不得以字串組 SQL。

## 6. Schema（初版 migration `0001_initial.sql`）

`CHECK` 僅用於固定列舉；應用層仍需做完整業務驗證與權限驗證。

```sql
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
```

### 設計說明

- `sort_key` 採可在相鄰值中插入的 LexoRank 類字串，避免整批改寫排序；服務需提供 `rank_between(before, after)`。若空間不足，在單一交易內重排該同層。
- 設定繼承沿 `projects.parent_id` 往上查詢，最近非 NULL 值優先；`prompt` 由根到葉串接。保留與現有 `Workspace.resolve()`／`instructions_for()` 相同語意。
- `metadata_json` 保存未知或未來 runner event 欄位；固定且需要查詢的欄位才增加正式 column。
- 20 MiB 以下的檔案以 1 MiB chunk 寫入 `attachment_chunks`。這仍是 SQLite BLOB 儲存，但可串流上傳／下載、限制記憶體峰值，並避免單一巨大 SQL parameter。
- 附件用 SHA-256 加大小去重。相同實體檔案只存一次，可掛到多則訊息。刪除關聯後不立即刪 bytes，交由保留期清理工作。
- `attachment_uploads`／`attachment_upload_chunks` 是未提交上傳的暫存區，絕不可被正常附件下載 endpoint 讀取。commit 在一個 writer transaction 內驗證連續 chunk 編號、總長與 SHA-256，再去重／建立 `attachments` 和 `message_attachments`，最後刪除暫存 chunks。

## 7. 搜尋與軟刪除規則

服務層在同一個交易內同步維護三張 FTS 表：新增／更新 live project、conversation、message 時，以 `project_id`／`conversation_id`／`message_id` 這個唯一 ID `DELETE` 舊列再 `INSERT` 新列；軟刪除時刪除 FTS 列；復原時重建列。不要讓 FTS trigger 自行推算專案 ID，因為對話移動到其他專案後需重新索引該 conversation 與其所有 live message。禁止直接用 FTS `rowid` 作為主資料 ID 或唯一性保證。

搜尋 endpoint 必須先以權限篩選可見專案，再執行 FTS，最後再次檢查主表 `deleted_at_ms IS NULL`。結果以類型、title、專案路徑、摘要及可安全開啟的 ID 回傳；工具輸出不預設列入全文搜尋，除非未來加入獨立選項。

軟刪除：

1. 刪除 project 時，在同一交易遞迴標記其 live 子專案、對話、訊息為 deleted，並自 FTS 移除。
2. 刪除 conversation 時同樣標記其訊息；附件僅解除可見關聯，不刪 BLOB。
3. `restore` 需檢查原父專案仍存在且使用者有 write 權限；同層名稱衝突時拒絕並要求先改名。
4. 只允許 admin 執行 30 天後的永久清理。永久清理前先確認沒有任何 live `message_attachments` 引用；未引用附件再刪 chunks 與 metadata。

`message_attachments` 在軟刪除時**保留**，以便 restore 恢復所有附件；「live 引用」定義為其 message 與 conversation 皆未軟刪除。永久清理須按依賴反向順序執行：先刪 FTS／idempotency 過期列，再刪 tool calls、message_attachments、messages，接著 conversations（先將其他 conversations 的 `forked_from_conversation_id` 設 NULL 並把原 ID 保留在 external 欄位）、memberships、projects；附件僅在不存在任何 live 或仍在 30 天保留期內的 message_attachments 引用時才刪 chunks 與 metadata。所有永久清理均寫 audit log（可先使用服務不可竄改的結構化日誌）。

## 8. API 與服務層契約

使用 HTTPS JSON API；附件上傳／下載可採 streamed request/response。最低限度 endpoint：

| 類別 | Endpoint／操作 |
| --- | --- |
| Auth | login、logout、me、admin 建立／停用使用者、重設密碼 |
| Projects | list tree、create、update、move、soft-delete、restore、memberships |
| Conversations | list／create／update／move／fork／soft-delete／restore、messages 分頁讀取 |
| Messages | 建立 user message、append/complete agent event、取消 turn、列出 tool calls |
| Attachments | initiate upload、順序上傳 chunks、commit、download、detach |
| Search | 以 FTS5 搜尋 project/conversation/message，具 type filter 與 pagination |

登入密碼採 Argon2id 雜湊；session token 為至少 32 bytes 的隨機值，資料庫只存 SHA-256 雜湊，並以 `HttpOnly`、`Secure`、`SameSite=Lax` cookie 或 OS keychain 保存。所有寫入 API 都接受 `Idempotency-Key`；另建 `idempotency_keys(user_id, key, response_json, expires_at_ms)` 表以保證網路重試不會重複建立訊息／附件。

Idempotency 實作：request fingerprint 是 method、route、canonical JSON body 與 upload target 的 SHA-256。同 user/key 再次到達且 fingerprint 相同時回放已保存的 `status_code`／`response_json`；fingerprint 不同回 `409`。同 key 並發由 `(user_id, request_key)` 主鍵與 writer queue 序列化；只保存已完成的成功或可安全回放的 4xx 回應，不保存暫時性 5xx。預設 24 小時到期。

附件流程：服務驗證名稱、MIME、宣告 size ≤ 20 MiB → 建立 `attachment_uploads` → 依 0 起算、不可重複的 chunk number streamed 寫入暫存列 → commit 時驗證連續 chunk、總長與 SHA-256 → transaction 將附件設為可用並關聯 message。未完成 upload 與其 chunks 在 24 小時後標為 expired 並清除。下載依序 streaming chunks，回傳正確的 `Content-Type`、`Content-Disposition: attachment`、大小與 hash；禁止由使用者提供檔案路徑。

## 9. 舊資料遷移

新增 `tree_agent/migrations/legacy_workspace_import.py`，只在管理員明確執行時匯入；不可自動覆蓋現有資料庫。

1. 先複製原 `workspace.json` 與 `attachments/` 到唯讀備份。
2. 驗證 JSON；缺失 `id` 產生 UUID、缺失 timestamps 採匯入當下時間、無效／未知 role 設為 `notice` 並保存原值於 `metadata_json`。深度優先建立 projects，保留兄弟順序為 `sort_key`。
3. 對每個 conversation 建立 row，映射 `thread_id`、`agent_id`、`claude_session_id`、fork 資訊與 timestamps；未知 agent 設為 `codex` 並記錄 warning。原 JSON 子節點違反「conversation 為 leaf」時拒絕該檔案而不寫入。
4. 依原 messages 順序寫入 `messages`；將 `images` 中存在的每個檔案讀入 BLOB chunks、建立 message attachment；遺失檔案、無法讀取檔案與不合法 size 記入遷移報告但不中斷其他資料。
5. 根專案與既有資料指派給執行匯入的管理員，建立 owner membership。
6. 驗證 project/conversation/message/attachment 數量與抽樣 SHA-256；完成後寫入 `migration_reports` 的 summary、issues 與 status，不刪舊檔。

舊 JSON 的 `ui`、本機 runner 路徑與本機草稿不屬共用工作區資料；另設使用者偏好設定表或客戶端本機設定保存。

## 10. 維運、容量與備份

- 100 GiB BLOB 資料庫需要至少 2.5 倍可用磁碟空間：主 DB、WAL 尖峰及備份／還原暫存。服務主機應使用加密磁碟與受限帳號。
- 每日使用 SQLite online backup API 產生一致性備份；保留至少 7 日每日、4 週每週與 12 個月每月備份。每次備份完成做 `PRAGMA integrity_check` 與可還原演練。
- WAL 可能因長時間 reader 無法 checkpoint 而成長。讀取 endpoint 必須分頁且及時關閉 cursor；監控 DB、`-wal`、`-shm` 檔大小與 writer queue 延遲。
- 不在日常排程執行 `VACUUM`；100 GiB 資料庫會需要接近同量暫存空間並鎖住寫入。只在維護窗口、完成備份後執行；若需要逐步回收空間，評估 `auto_vacuum=INCREMENTAL`（必須在初始建立前決定）。
- 對資料庫檔與備份進行 OS 層加密，服務日誌禁止記錄 message 原文、附件 bytes、session token 或密碼。

## 11. 實作順序（給 Claude Code）

1. 新建服務端 package：SQLite connection factory、migration runner、repository interface、domain errors、writer queue。
2. 先實作 migration 與 repository 單元測試；此階段不修改 Tk UI 行為。
3. 實作 users、login/session、project membership middleware，以及第一位 admin bootstrap。
4. 實作 projects/conversations/messages/tool calls，將既有 `tree_agent/store.py` 的 traversal、繼承、命名、move、fork 規則搬到 service 層；所有查詢強制權限過濾。
5. 實作 chunked attachments、hash 去重、引用計數式清理與下載串流。
6. 實作 FTS 寫入同步與 search endpoint，加入中文、英文與混合查詢測試。
7. 將桌面程式的 `Workspace` 呼叫改成 API client；移除所有客戶端直接讀寫 `workspace.json` 的路徑。可在過渡期保留唯讀匯入工具。
8. 執行舊 workspace 匯入，核對資料，部署備份與監控，再切換使用者。

禁止 Claude Code：直接將 SQLite 放到 network share、在每台桌面客戶端開啟 DB、以明文保存密碼／token、用 blob 一次讀入 20 MiB 到記憶體、或以硬刪除取代軟刪除。

專案移動必須在同一 transaction 中驗證：目的 parent 為 live project、操作者對來源與目的均有 editor 權限、目的不是自己且不是自己的任何 descendant、名稱不與目的同層 live node 衝突。任何遞迴查詢都應帶循環偵測／最大深度保護；正常寫入不得產生循環。

## 12. 驗收測試

- 兩位 member 同時新增／排序／移動同一專案，revision 衝突必須可預測地得到 `409`，資料不遺失。
- viewer 能搜尋與讀取授權專案，但無法修改、上傳、下載未授權附件，且無法以 ID 猜測取得資料。
- project 設定繼承、prompt 累加、對話 fork、agent/tool event 順序與現有行為一致。
- 20 MiB 附件可上傳、下載、hash 驗證、重啟後仍可用；20 MiB + 1 byte 必須被拒絕；相同檔案不重複儲存。
- 軟刪除後 UI、list API 與 FTS 都不可見；admin 可在無命名衝突時復原；清理工作只清除到期且未被引用的資料。
- 模擬服務中斷與 client retry，訊息／附件只建立一次；migration 在錯誤時 rollback。
- 對含 project tree、messages、圖片附件、tool output 的舊工作區執行匯入，數量及抽樣 checksum 與來源一致。
- 備份後在隔離環境還原，執行 `integrity_check`、登入、搜尋、下載附件均成功。

## 13. 建議的檔案布局

```text
tree_agent/
  server/
    app.py
    db.py
    migrations/
      0001_initial.sql
      legacy_workspace_import.py
    repositories/
    services/
    auth.py
    api.py
  client_api.py
  store.py                 # 過渡期 adapter；最終移除 JSON persistence
docs/
  sqlite-storage-implementation-spec.md
```

在開始改碼前，Claude Code 應先讀本文件與現有 `tree_agent/store.py`、`tree_agent/transfer.py`，並先提出 framework（例如 FastAPI）與部署方式的最小差異方案；不得未經確認任意加入雲端資料庫或即時推播。
