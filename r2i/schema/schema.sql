-- The frozen ledger schema, version 1. A new ledger is created from this file; later changes go in migrations/NNNN_*.sql
-- AND here (so a new ledger and a migrated one end up the same). CHECK lists must equal the constants in r2i/db.py and
-- r2i/experiments.py.
CREATE TABLE mainlines (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, short TEXT NOT NULL);
CREATE TABLE stops (
  id TEXT PRIMARY KEY, target TEXT NOT NULL, reason TEXT NOT NULL, words TEXT, editor TEXT NOT NULL,
  created_at TEXT NOT NULL, lifted_at TEXT, lifted_by TEXT);
CREATE TABLE goals (
  mainline TEXT NOT NULL REFERENCES mainlines(id),
  version INTEGER NOT NULL,
  goal TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('待定义','生效','已取代')),
  trigger_evidence TEXT, approved_by TEXT, approved_at TEXT, created_at TEXT NOT NULL,
  PRIMARY KEY (mainline, version));
CREATE TABLE criteria (
  id TEXT PRIMARY KEY,
  mainline TEXT NOT NULL REFERENCES mainlines(id),
  goal_version INTEGER NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '未验证' CHECK (status IN ('未验证','部分','满足','被反证')),
  evidence TEXT, updated_at TEXT NOT NULL,
  -- the reading drawn as a bar on the 进度 page (crit-read)
  short TEXT, reading REAL, reading_label TEXT, target REAL, target_label TEXT, scale_max REAL, direction TEXT,
  reading_kind TEXT, reading_note TEXT, reading_at TEXT);
CREATE TABLE milestones (
  id TEXT PRIMARY KEY,
  date TEXT NOT NULL,
  mainline TEXT NOT NULL REFERENCES mainlines(id),
  kind TEXT NOT NULL CHECK (kind IN ('里程碑','正结果','负结果','大跑完成','版本变更','pivot','实战教训')),
  summary TEXT NOT NULL,
  impact TEXT,
  exp_id TEXT, evidence TEXT, cost TEXT, duration TEXT,
  goal_version INTEGER, editor TEXT NOT NULL, superseded_by TEXT,
  created_at TEXT NOT NULL,
  seen_at TEXT, sync_page TEXT);  -- seen_at: the owner ticked 已看 on its 同步 page; sync_page: 1 = ms-add --sync-page, done = skeleton written
CREATE TABLE milestone_reviews (
  exp_id TEXT PRIMARY KEY,
  decision TEXT NOT NULL CHECK (decision IN ('milestone', 'skip')),
  milestone_id TEXT, reviewer TEXT NOT NULL, reviewed_at TEXT NOT NULL);
CREATE TABLE decisions (
  id TEXT PRIMARY KEY,
  mainline TEXT NOT NULL REFERENCES mainlines(id),
  question TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待决' CHECK (status IN ('待决','已决','撤回')),
  answer TEXT, decided_at TEXT, source TEXT, created_at TEXT NOT NULL,
  -- thread: who asked (cc_...); answered_via: 首页 / 线程; acted_by: the acting thread's title
  title TEXT, options TEXT, thread TEXT, answered_via TEXT, acted_by TEXT, detail TEXT);
CREATE TABLE resources (
  service TEXT PRIMARY KEY,
  kind TEXT, unit TEXT, balance TEXT, limit_note TEXT, reset_at TEXT,
  source TEXT, checked_at TEXT,
  category TEXT, purpose TEXT, credential_ref TEXT, check_method TEXT, runs_on TEXT);
CREATE TABLE waits (
  id TEXT PRIMARY KEY,
  thread TEXT NOT NULL, ask TEXT NOT NULL, editor TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '等待' CHECK (status IN ('等待','已回','已勾','过期')),
  asked_at TEXT NOT NULL, notified_at TEXT, closed_at TEXT,
  closed_by TEXT, answer TEXT, report TEXT, typed TEXT, src TEXT);
CREATE TABLE report_replies (
  id TEXT PRIMARY KEY,
  page TEXT NOT NULL, wait_id TEXT, choice TEXT, text TEXT, page_mtime REAL, received_at TEXT NOT NULL,
  thread TEXT, delivered_at TEXT, kind TEXT, owner_msg TEXT, done_at TEXT);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);  -- key/value (JSON values): schema_version, created, mode:NAME, cursors
CREATE TABLE mode_state (name TEXT PRIMARY KEY, changed_at TEXT, observe_until TEXT);  -- the mode itself stays in meta 'mode:NAME'
CREATE TABLE night_items (
  id TEXT PRIMARY KEY,
  mainline TEXT NOT NULL REFERENCES mainlines(id),
  task TEXT NOT NULL,
  cap_cny REAL NOT NULL CHECK (cap_cny > 0),
  stop_if TEXT NOT NULL,
  deadline TEXT NOT NULL,
  exp_id TEXT, editor TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '待批' CHECK (status IN ('待批','已批','撤回','过期')),
  decided_at TEXT, budget_written_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE verbal_budgets (
  id TEXT PRIMARY KEY,
  cap_cny REAL NOT NULL CHECK (cap_cny > 0),
  purpose TEXT NOT NULL,
  approval_msg TEXT NOT NULL, approval_at TEXT NOT NULL, approval_quote TEXT NOT NULL,
  expires_at TEXT NOT NULL, editor TEXT NOT NULL, created_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT '有效' CHECK (status IN ('有效','撤回')),
  revoked_at TEXT, revoked_by TEXT, revoke_reason TEXT,
  budget_written_at TEXT, revoke_written_at TEXT, error TEXT,
  basis TEXT, audit_id TEXT, approval_reply TEXT);  -- basis/audit_id: the small-spend route; approval_reply: the report_replies row a tick-approved budget came from
CREATE TABLE exp_notes (
  code TEXT PRIMARY KEY,
  plain_name TEXT, data_layer TEXT, editor TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE task_notes (
  id TEXT PRIMARY KEY,
  date TEXT NOT NULL,
  mainline TEXT NOT NULL REFERENCES mainlines(id),
  title TEXT NOT NULL,
  did TEXT NOT NULL, result TEXT NOT NULL, meaning TEXT NOT NULL,
  detail TEXT, data TEXT,
  limits TEXT, exp_ids TEXT, cost TEXT, duration TEXT, links TEXT,
  editor TEXT NOT NULL, created_at TEXT NOT NULL, step TEXT,
  sync TEXT);  -- 1 = render writes/updates its 同步 page, done, no; NULL = older rows
CREATE TABLE structures (
  mainline TEXT NOT NULL REFERENCES mainlines(id),
  version INTEGER NOT NULL,
  approved_by TEXT NOT NULL, evidence TEXT NOT NULL, editor TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY (mainline, version));
CREATE TABLE audits (
  id TEXT PRIMARY KEY,
  mainline TEXT NOT NULL REFERENCES mainlines(id),
  experiment TEXT, title TEXT NOT NULL, crit TEXT, before TEXT, after TEXT,
  verdict TEXT, grade TEXT, cost_cny REAL,
  kind TEXT NOT NULL CHECK (kind IN ('result','decided')),
  decision TEXT, evidence TEXT, editor TEXT NOT NULL, thread TEXT,
  created_at TEXT NOT NULL, undo_status TEXT NOT NULL DEFAULT '',
  rules_version TEXT, recheck TEXT, subject TEXT, on_crit_set TEXT, note TEXT);
CREATE TABLE ideas (
  id TEXT PRIMARY KEY,
  mainline TEXT NOT NULL REFERENCES mainlines(id),
  content TEXT NOT NULL, source_exp TEXT,
  status TEXT NOT NULL DEFAULT '新' CHECK (status IN ('新','在试','已证伪','已采纳')),
  ra_checked INTEGER NOT NULL DEFAULT 0 CHECK (ra_checked IN (0,1)),
  deploy_data TEXT, editor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE hypotheses (
  id TEXT PRIMARY KEY,
  mainline TEXT NOT NULL REFERENCES mainlines(id),
  content TEXT NOT NULL, source_exp TEXT,
  status TEXT NOT NULL DEFAULT '新' CHECK (status IN ('新','在试','已证伪','已采纳')),
  ra_checked INTEGER NOT NULL DEFAULT 0 CHECK (ra_checked IN (0,1)),
  deploy_data TEXT, editor TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL, editor TEXT NOT NULL, path TEXT NOT NULL, summary TEXT NOT NULL);
CREATE TABLE experiments (
  local_id TEXT PRIMARY KEY,
  title TEXT, date_start TEXT, date_end TEXT, department TEXT, stage TEXT, verdict TEXT,
  observation TEXT, cost TEXT, path TEXT, next_check TEXT, raw_record TEXT,
  mainline TEXT, completed_at TEXT, crit TEXT, reviewer TEXT, review_due TEXT, updated_at TEXT);  -- mainline/department hold JSON lists, so no FK
CREATE TABLE threads (  -- one row per Claude Code background agent thread; agent = the dispatcher's managed-agent id/name (SendMessage target)
  id TEXT PRIMARY KEY /* cc_<uuid> */, title TEXT NOT NULL, kind TEXT, model TEXT, mainline TEXT REFERENCES mainlines(id),
  agent TEXT, status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')), created_at TEXT NOT NULL, closed_at TEXT);
CREATE TABLE msgs (  -- messages between threads (used by later steps)
  id INTEGER PRIMARY KEY AUTOINCREMENT, to_thread TEXT NOT NULL, from_thread TEXT, body TEXT NOT NULL,
  created_at TEXT NOT NULL, delivered_at TEXT);
CREATE TABLE owner_msgs (  -- the owner's chat messages, seen by session (used by later steps)
  id TEXT PRIMARY KEY /* uuid */, session TEXT NOT NULL, agent TEXT, prompt_sha TEXT NOT NULL,
  is_owner INTEGER NOT NULL CHECK (is_owner IN (0,1)), created_at TEXT NOT NULL);
