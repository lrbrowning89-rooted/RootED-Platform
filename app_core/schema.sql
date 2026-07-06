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
  FOREIGN KEY (student_id)  REFERENCES students(student_id)   ON DELETE CASCADE,
  FOREIGN KEY (question_id) REFERENCES questions(question_id) ON DELETE CASCADE
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
