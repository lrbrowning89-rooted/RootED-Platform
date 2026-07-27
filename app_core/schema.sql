PRAGMA foreign_keys = ON;

-- -------------------------
-- Config
-- -------------------------
CREATE TABLE IF NOT EXISTS config (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- -------------------------
-- Standards & Objectives
-- -------------------------
CREATE TABLE IF NOT EXISTS standards (
  standard_id   TEXT PRIMARY KEY,
  standard_name TEXT,           -- optional human-readable label
  core_idea     TEXT,           -- e.g., LS1, PS, ESS
  grade_band    TEXT            -- e.g., MS, HS
);

CREATE TABLE IF NOT EXISTS objectives (
  objective_id   TEXT PRIMARY KEY,
  standard_id    TEXT NOT NULL,
  objective_text TEXT,
  display_name   TEXT,
  student_description TEXT,
  order_in_band  INTEGER,
  FOREIGN KEY (standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE
);

-- Optional mapping of objective → level (1/2/3) used by dashboard & engine
CREATE TABLE IF NOT EXISTS objective_levels (
  objective_id TEXT PRIMARY KEY,
  level        INTEGER NOT NULL
);

-- -------------------------
-- Questions & Skills
-- -------------------------
CREATE TABLE IF NOT EXISTS questions (
  question_id  TEXT PRIMARY KEY,
  objective_id TEXT NOT NULL,
  stem         TEXT,
  choice_a     TEXT,
  choice_b     TEXT,
  choice_c     TEXT,
  choice_d     TEXT,
  answer_key   TEXT,
  reading_level TEXT,  -- optional: e.g., "6" for 6th-grade readability
  FOREIGN KEY (objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS skills (
  skill_id    TEXT PRIMARY KEY,
  skill_name  TEXT NOT NULL,
  description TEXT
);

CREATE TABLE IF NOT EXISTS question_skills (
  question_id TEXT NOT NULL,
  skill_id    TEXT NOT NULL,
  PRIMARY KEY (question_id, skill_id),
  FOREIGN KEY (question_id) REFERENCES questions(question_id) ON DELETE CASCADE,
  FOREIGN KEY (skill_id)    REFERENCES skills(skill_id)    ON DELETE CASCADE
);

-- -------------------------
-- Model Assets
-- -------------------------
CREATE TABLE IF NOT EXISTS model_assets (
  model_id   TEXT PRIMARY KEY CHECK (TRIM(model_id) <> ''),
  asset_type TEXT NOT NULL CHECK (asset_type IN ('image')),
  src        TEXT NOT NULL CHECK (TRIM(src) <> ''),
  alt_text   TEXT NOT NULL,
  title      TEXT,
  caption    TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

-- -------------------------
-- Students & Attempts
-- -------------------------
CREATE TABLE IF NOT EXISTS students (
  student_id   TEXT PRIMARY KEY,
  first_name   TEXT,
  last_name    TEXT,
  grade        INTEGER,
  class_period TEXT
);

CREATE TABLE IF NOT EXISTS class_sections (
  class_id        TEXT PRIMARY KEY,
  teacher_user_id INTEGER,
  name            TEXT NOT NULL,
  class_period    TEXT,
  join_code       TEXT UNIQUE NOT NULL,
  is_active       INTEGER NOT NULL DEFAULT 1,
  created_at      INTEGER NOT NULL,
  updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS class_enrollments (
  class_id    TEXT NOT NULL,
  student_id  TEXT NOT NULL,
  enrolled_at INTEGER NOT NULL,
  enrolled_by TEXT NOT NULL DEFAULT 'self',
  is_active   INTEGER NOT NULL DEFAULT 1,
  archived_at INTEGER,
  archived_by_user_id INTEGER,
  archive_reason TEXT,
  reactivated_at INTEGER,
  reactivated_by_user_id INTEGER,
  PRIMARY KEY (class_id, student_id),
  FOREIGN KEY (class_id) REFERENCES class_sections(class_id) ON DELETE CASCADE,
  FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_class_enrollments_student
  ON class_enrollments(student_id);

CREATE TABLE IF NOT EXISTS attempts (
  attempt_id    TEXT PRIMARY KEY,
  student_id    TEXT NOT NULL,
  question_id   TEXT NOT NULL,
  timestamp     INTEGER NOT NULL,
  response      TEXT,
  is_correct    INTEGER NOT NULL,  -- 1/0
  time_seconds  REAL,
  skills_missed TEXT,
  FOREIGN KEY (student_id)  REFERENCES students(student_id)   ON DELETE CASCADE,
  FOREIGN KEY (question_id) REFERENCES questions(question_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_attempts_student_obj_ts
  ON attempts(student_id, question_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS question_flags (
  flag_id             TEXT PRIMARY KEY,
  question_id         TEXT NOT NULL,
  objective_id        TEXT NOT NULL,
  standard_id         TEXT NOT NULL,
  reporter_user_id    INTEGER,
  reporter_role       TEXT NOT NULL CHECK (reporter_role IN ('teacher', 'student')),
  class_id            TEXT,
  student_id          TEXT,
  category            TEXT NOT NULL,
  comment             TEXT,
  page_context        TEXT,
  created_ts          INTEGER NOT NULL,
  status              TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'teacher_resolved', 'escalated', 'owner_reviewing', 'fixed', 'closed')),
  escalated_by_user_id INTEGER,
  escalated_at        INTEGER,
  escalation_note     TEXT,
  owner_reviewing_at  INTEGER,
  owner_reviewing_by_user_id INTEGER,
  resolved_ts         INTEGER,
  resolution_note     TEXT,
  resolved_by_user_id INTEGER,
  FOREIGN KEY (question_id) REFERENCES questions(question_id) ON DELETE CASCADE,
  FOREIGN KEY (objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE,
  FOREIGN KEY (standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY (class_id) REFERENCES class_sections(class_id) ON DELETE SET NULL,
  FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_question_flags_status_created
  ON question_flags(status, created_ts DESC);

CREATE INDEX IF NOT EXISTS idx_question_flags_question_status
  ON question_flags(question_id, status);

CREATE TABLE IF NOT EXISTS question_flag_events (
  event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
  flag_id         TEXT NOT NULL,
  from_status     TEXT,
  to_status       TEXT NOT NULL CHECK (to_status IN ('open', 'teacher_resolved', 'escalated', 'owner_reviewing', 'fixed', 'closed')),
  actor_user_id   INTEGER NOT NULL,
  actor_authority TEXT NOT NULL,
  note            TEXT,
  created_ts      INTEGER NOT NULL,
  FOREIGN KEY (flag_id) REFERENCES question_flags(flag_id) ON DELETE CASCADE,
  FOREIGN KEY (actor_user_id) REFERENCES users(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_question_flag_events_flag_created
  ON question_flag_events(flag_id, created_ts, event_id);

-- -------------------------
-- Platform authority
-- -------------------------
CREATE TABLE IF NOT EXISTS user_platform_roles (
  grant_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL,
  platform_role TEXT NOT NULL CHECK (platform_role IN ('owner')),
  granted_at    INTEGER NOT NULL,
  granted_by    INTEGER,
  revoked_at    INTEGER,
  revoked_by    INTEGER,
  grant_note    TEXT,
  revoke_note   TEXT,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY (granted_by) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY (revoked_by) REFERENCES users(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_platform_roles_active
  ON user_platform_roles(user_id, platform_role)
  WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_user_platform_roles_history
  ON user_platform_roles(platform_role, revoked_at, user_id, granted_at);

-- -------------------------
-- Instructional authorization
-- -------------------------
CREATE TABLE IF NOT EXISTS user_instructional_authorizations (
  authorization_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id            INTEGER NOT NULL,
  instructional_role TEXT NOT NULL CHECK(instructional_role IN ('teacher')),
  granted_at         INTEGER NOT NULL,
  granted_by         INTEGER,
  revoked_at         INTEGER,
  revoked_by         INTEGER,
  grant_note         TEXT,
  revoke_note        TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(granted_by) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY(revoked_by) REFERENCES users(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_user_instructional_authorizations_active
  ON user_instructional_authorizations(user_id, instructional_role)
  WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_user_instructional_authorizations_history
  ON user_instructional_authorizations(
    instructional_role, revoked_at, user_id, granted_at
  );

CREATE TABLE IF NOT EXISTS teacher_email_authorizations (
  email_authorization_id INTEGER PRIMARY KEY AUTOINCREMENT,
  normalized_email TEXT NOT NULL,
  display_name TEXT,
  user_id INTEGER,
  instructional_authorization_id INTEGER,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending', 'active', 'deactivated', 'revoked')),
  created_at INTEGER NOT NULL,
  created_by INTEGER NOT NULL,
  claimed_at INTEGER,
  deactivated_at INTEGER,
  deactivated_by INTEGER,
  revoked_at INTEGER,
  revoked_by INTEGER,
  internal_note TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(instructional_authorization_id)
    REFERENCES user_instructional_authorizations(authorization_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_teacher_email_authorizations_open
  ON teacher_email_authorizations(normalized_email)
  WHERE status IN ('pending', 'active', 'deactivated');

CREATE TABLE IF NOT EXISTS access_authorization_audit_log (
  audit_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_user_id     INTEGER NOT NULL,
  target_user_id    INTEGER NOT NULL,
  action            TEXT NOT NULL CHECK(action IN ('teacher_authorized')),
  authorization_id  INTEGER,
  outcome           TEXT NOT NULL CHECK(outcome IN ('granted', 'already_granted')),
  created_at        INTEGER NOT NULL,
  request_ip        TEXT,
  user_agent        TEXT,
  FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(target_user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(authorization_id)
    REFERENCES user_instructional_authorizations(authorization_id)
    ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_access_authorization_audit_target
  ON access_authorization_audit_log(target_user_id, created_at, audit_id);

CREATE TABLE IF NOT EXISTS authorization_lifecycle_audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_user_id INTEGER NOT NULL,
  target_user_id INTEGER NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('teacher_revoked')),
  authorization_id INTEGER,
  outcome TEXT NOT NULL CHECK(outcome IN ('revoked', 'already_revoked', 'blocked_active_classes')),
  reason TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS account_lifecycle_audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_user_id INTEGER NOT NULL,
  target_user_id INTEGER NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('account_deactivated', 'account_reactivated')),
  outcome TEXT NOT NULL CHECK(outcome IN ('deactivated', 'reactivated', 'already_deactivated', 'already_active', 'blocked_last_owner')),
  reason TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS class_membership_audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor_user_id INTEGER NOT NULL,
  class_id TEXT NOT NULL,
  student_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('membership_archived')),
  outcome TEXT NOT NULL CHECK(outcome IN ('archived', 'already_archived')),
  reason TEXT,
  created_at INTEGER NOT NULL
);

-- -------------------------
-- Teacher-authored maps & graph
-- -------------------------
CREATE TABLE IF NOT EXISTS lists (
  objective_id   TEXT PRIMARY KEY,
  advance_to     TEXT,
  remediation_to TEXT,
  FOREIGN KEY (objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS paths (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  from_node TEXT NOT NULL,
  to_node   TEXT NOT NULL,
  note      TEXT
  -- No foreign keys enforced here because nodes may be cross-domain
);

-- -------------------------
-- Cross-band promotion rules
-- (used by decide_next in dashboard_v5)
-- -------------------------
CREATE TABLE IF NOT EXISTS progression_rules (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  core_idea         TEXT NOT NULL,
  from_band         TEXT NOT NULL,
  to_band           TEXT NOT NULL,
  promote_threshold REAL NOT NULL
);

-- -------------------------
-- Standard-level responses (Rolling-7 data)
-- -------------------------
CREATE TABLE IF NOT EXISTS responses (
  id          INTEGER PRIMARY KEY,
  student_id  TEXT NOT NULL,
  standard_id TEXT NOT NULL,     -- e.g., "MS-LS1-1"
  level       INTEGER NOT NULL DEFAULT 1,  -- 1 = recall, 2 = application, 3 = reasoning
  question_id TEXT NOT NULL,
  correct     INTEGER NOT NULL,           -- 1 or 0
  ts          INTEGER NOT NULL,
  routing_level_attempt_id TEXT,
  FOREIGN KEY (student_id)  REFERENCES students(student_id)   ON DELETE CASCADE,
  FOREIGN KEY (question_id) REFERENCES questions(question_id) ON DELETE CASCADE,
  FOREIGN KEY (routing_level_attempt_id) REFERENCES routing_level_attempts(attempt_id)
);

CREATE INDEX IF NOT EXISTS idx_responses_student_std_lvl_ts
  ON responses(student_id, standard_id, level, ts DESC);

-- -------------------------
-- Frustration logging (optional, future use)
-- -------------------------
CREATE TABLE IF NOT EXISTS frustration_events (
  event_id     TEXT PRIMARY KEY,
  student_id   TEXT NOT NULL,
  objective_id TEXT NOT NULL,
  timestamp    INTEGER NOT NULL,
  trigger      TEXT NOT NULL, -- e.g., '5_recent_wrongs'
  FOREIGN KEY (student_id)   REFERENCES students(student_id)    ON DELETE CASCADE,
  FOREIGN KEY (objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
);

-- ======================================================================
-- Rolling-7 Adaptive Engine Tables
-- (used by adaptive_engine.py)
-- ======================================================================

-- Tracks current standard-level state for each student
CREATE TABLE IF NOT EXISTS progress_state (
  id            INTEGER PRIMARY KEY,
  student_id    TEXT NOT NULL,
  standard_id   TEXT NOT NULL,
  current_level INTEGER NOT NULL DEFAULT 1,
  status        TEXT NOT NULL DEFAULT 'practicing',  -- practicing | mastered | remediation
  rolling_avg   REAL NOT NULL DEFAULT 0.0,
  locked        INTEGER NOT NULL DEFAULT 0,          -- 0 = false, 1 = true
  locked_reason TEXT,
  last_update   INTEGER NOT NULL,
  FOREIGN KEY (student_id)  REFERENCES students(student_id)   ON DELETE CASCADE,
  FOREIGN KEY (standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_progress_state_unique
  ON progress_state(student_id, standard_id);

-- Authoritative evidence boundary for cumulative mastery at one routing level.
CREATE TABLE IF NOT EXISTS routing_level_attempts (
  attempt_id TEXT PRIMARY KEY,
  student_id TEXT NOT NULL,
  standard_id TEXT NOT NULL,
  objective_id TEXT NOT NULL,
  level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 3),
  status TEXT NOT NULL DEFAULT 'active'
    CHECK(status IN ('active', 'closed', 'invalidated')),
  started_at INTEGER NOT NULL,
  ended_at INTEGER,
  end_reason TEXT,
  final_correct_count INTEGER,
  final_response_count INTEGER,
  final_score REAL,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_routing_level_attempt_active_student
  ON routing_level_attempts(student_id) WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_responses_routing_level_attempt
  ON responses(routing_level_attempt_id, id);

-- Presentation-only growth state. It never drives adaptive routing.
CREATE TABLE IF NOT EXISTS student_growth_progress (
  attempt_id TEXT PRIMARY KEY,
  student_id TEXT NOT NULL,
  standard_id TEXT NOT NULL,
  objective_id TEXT NOT NULL,
  visible_stage INTEGER NOT NULL DEFAULT 1 CHECK(visible_stage BETWEEN 1 AND 4),
  presentation_state TEXT NOT NULL DEFAULT 'strengthening'
    CHECK(presentation_state IN ('growing', 'strengthening', 'reviewing', 'complete')),
  is_active INTEGER NOT NULL DEFAULT 1,
  started_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  completed_at INTEGER,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_student_growth_active
  ON student_growth_progress(student_id) WHERE is_active = 1;

CREATE TABLE IF NOT EXISTS student_question_deliveries (
  submission_token TEXT PRIMARY KEY,
  student_id TEXT NOT NULL,
  growth_attempt_id TEXT NOT NULL,
  standard_id TEXT NOT NULL,
  objective_id TEXT NOT NULL,
  level INTEGER NOT NULL,
  question_id TEXT NOT NULL,
  sequence_number INTEGER NOT NULL,
  served_at INTEGER NOT NULL,
  consumed_at INTEGER,
  invalidated_at INTEGER,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(growth_attempt_id) REFERENCES student_growth_progress(attempt_id) ON DELETE CASCADE,
  FOREIGN KEY(question_id) REFERENCES questions(question_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_student_question_delivery_active
  ON student_question_deliveries(student_id)
  WHERE consumed_at IS NULL AND invalidated_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_student_question_delivery_sequence
  ON student_question_deliveries(growth_attempt_id, level, sequence_number);

-- Links MS standard → lower-band remediation standards (3–5, then K–2, etc.)
CREATE TABLE IF NOT EXISTS remediation_links (
  id               INTEGER PRIMARY KEY,
  standard_id      TEXT NOT NULL,        -- e.g., 'MS-LS1-1'
  lower_standard_id TEXT NOT NULL,       -- e.g., '5-LS1-1'
  FOREIGN KEY (standard_id)       REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY (lower_standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE
);

-- Links each standard to its "next up" standard for continuous progression
CREATE TABLE IF NOT EXISTS progression_links (
  id              INTEGER PRIMARY KEY,
  standard_id     TEXT NOT NULL,      -- current
  next_standard_id TEXT NOT NULL,     -- where to go after Level 3 mastery
  FOREIGN KEY (standard_id)      REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY (next_standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE
);

-- Remembers where a remediation standard was triggered from
CREATE TABLE IF NOT EXISTS origin_links (
  id               INTEGER PRIMARY KEY,
  student_id       TEXT NOT NULL,
  rem_standard_id  TEXT NOT NULL,   -- lower-band standard
  from_standard_id TEXT NOT NULL,   -- original standard where they dropped from
  from_level       INTEGER NOT NULL,
  ts               INTEGER NOT NULL,
  FOREIGN KEY (student_id)       REFERENCES students(student_id)   ON DELETE CASCADE,
  FOREIGN KEY (rem_standard_id)  REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY (from_standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE
);

-- Simple notifications table (drops, locks, etc.)
CREATE TABLE IF NOT EXISTS notifications (
  id          INTEGER PRIMARY KEY,
  student_id  TEXT NOT NULL,
  standard_id TEXT NOT NULL,
  event       TEXT NOT NULL,    -- 'drop', 'lock', etc.
  details     TEXT NOT NULL,
  ts          INTEGER NOT NULL,
  FOREIGN KEY (student_id)  REFERENCES students(student_id)   ON DELETE CASCADE,
  FOREIGN KEY (standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE
);
