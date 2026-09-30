PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS rooted_database_metadata (
  singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
  schema_version INTEGER NOT NULL,
  environment TEXT NOT NULL CHECK(environment IN ('development','test','production')),
  data_classification TEXT NOT NULL CHECK(data_classification IN ('synthetic','protected')),
  database_identifier TEXT NOT NULL CHECK(TRIM(database_identifier) <> ''),
  content_release TEXT, synthetic_fixture_version TEXT, created_at INTEGER NOT NULL,
  created_by_tool TEXT, canonical_schema_checksum TEXT
);
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS standards (
  standard_id TEXT PRIMARY KEY, standard_name TEXT, core_idea TEXT, grade_band TEXT
);
CREATE TABLE IF NOT EXISTS objectives (
  objective_id TEXT PRIMARY KEY, standard_id TEXT NOT NULL, objective_text TEXT,
  display_name TEXT, student_description TEXT, order_in_band INTEGER,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS objective_levels (
  objective_id TEXT PRIMARY KEY, level INTEGER NOT NULL,
  FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS questions (
  question_id TEXT PRIMARY KEY, objective_id TEXT NOT NULL, stem TEXT,
  choice_a TEXT, choice_b TEXT, choice_c TEXT, choice_d TEXT, answer_key TEXT,
  reading_level TEXT,
  FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS skills (
  skill_id TEXT PRIMARY KEY, skill_name TEXT NOT NULL, description TEXT
);
CREATE TABLE IF NOT EXISTS question_skills (
  question_id TEXT NOT NULL, skill_id TEXT NOT NULL, PRIMARY KEY(question_id,skill_id),
  FOREIGN KEY(question_id) REFERENCES questions(question_id) ON DELETE CASCADE,
  FOREIGN KEY(skill_id) REFERENCES skills(skill_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS model_assets (
  model_id TEXT PRIMARY KEY CHECK(TRIM(model_id) <> ''),
  asset_type TEXT NOT NULL CHECK(asset_type IN ('image')),
  src TEXT NOT NULL CHECK(TRIM(src) <> ''), alt_text TEXT NOT NULL,
  title TEXT, caption TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS question_metadata (
  question_id TEXT PRIMARY KEY, difficulty TEXT, question_type TEXT, explanation TEXT,
  identifier_tags TEXT, remediation_tags TEXT, model_id TEXT, source_artifact TEXT,
  imported_at INTEGER,
  FOREIGN KEY(question_id) REFERENCES questions(question_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS content_release_log (
  package_name TEXT NOT NULL, package_version TEXT NOT NULL,
  source_checksum TEXT NOT NULL, applied_at INTEGER NOT NULL,
  database_identifier TEXT NOT NULL, inserted_count INTEGER NOT NULL,
  updated_count INTEGER NOT NULL, unchanged_count INTEGER NOT NULL,
  verification_result TEXT NOT NULL, PRIMARY KEY(package_name,package_version)
);

CREATE TABLE IF NOT EXISTS students (
  student_id TEXT PRIMARY KEY, first_name TEXT, last_name TEXT,
  grade INTEGER, class_period TEXT
);
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('teacher','student')),
  account_role TEXT CHECK(account_role IN ('pending','student','teacher')),
  linked_student_id TEXT, is_active INTEGER NOT NULL DEFAULT 1,
  sso_provider TEXT, sso_subject TEXT, sso_email TEXT, last_login_ts INTEGER,
  FOREIGN KEY(linked_student_id) REFERENCES students(student_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_users_sso ON users(sso_provider,sso_subject);
CREATE TABLE IF NOT EXISTS user_auth_identities (
  identity_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
  provider TEXT NOT NULL, provider_subject TEXT NOT NULL, verified_email TEXT,
  display_name TEXT, avatar_url TEXT, created_at INTEGER NOT NULL,
  last_login_at INTEGER NOT NULL, revoked_at INTEGER,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
  UNIQUE(provider,provider_subject)
);
CREATE INDEX IF NOT EXISTS idx_auth_identities_email ON user_auth_identities(verified_email);
CREATE TABLE IF NOT EXISTS user_platform_roles (
  grant_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
  platform_role TEXT NOT NULL CHECK(platform_role IN ('owner')),
  granted_at INTEGER NOT NULL, granted_by INTEGER, revoked_at INTEGER,
  revoked_by INTEGER, grant_note TEXT, revoke_note TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(granted_by) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY(revoked_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_platform_roles_active
  ON user_platform_roles(user_id,platform_role) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_user_platform_roles_history
  ON user_platform_roles(platform_role,revoked_at,user_id,granted_at);
CREATE TABLE IF NOT EXISTS user_instructional_authorizations (
  authorization_id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
  instructional_role TEXT NOT NULL CHECK(instructional_role IN ('teacher')),
  granted_at INTEGER NOT NULL, granted_by INTEGER, revoked_at INTEGER,
  revoked_by INTEGER, grant_note TEXT, revoke_note TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(granted_by) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY(revoked_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_user_instructional_authorizations_active
  ON user_instructional_authorizations(user_id,instructional_role) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_user_instructional_authorizations_history
  ON user_instructional_authorizations(instructional_role,revoked_at,user_id,granted_at);
CREATE TABLE IF NOT EXISTS teacher_email_authorizations (
  email_authorization_id INTEGER PRIMARY KEY AUTOINCREMENT,
  normalized_email TEXT NOT NULL, display_name TEXT, user_id INTEGER,
  instructional_authorization_id INTEGER,
  status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','active','deactivated','revoked')),
  created_at INTEGER NOT NULL, created_by INTEGER NOT NULL, claimed_at INTEGER,
  deactivated_at INTEGER, deactivated_by INTEGER, revoked_at INTEGER,
  revoked_by INTEGER, internal_note TEXT,
  FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(instructional_authorization_id) REFERENCES user_instructional_authorizations(authorization_id) ON DELETE RESTRICT,
  FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(deactivated_by) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY(revoked_by) REFERENCES users(id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_teacher_email_authorizations_open
  ON teacher_email_authorizations(normalized_email) WHERE status IN ('pending','active','deactivated');
CREATE INDEX IF NOT EXISTS idx_teacher_email_authorizations_user
  ON teacher_email_authorizations(user_id,status);

CREATE TABLE IF NOT EXISTS class_sections (
  class_id TEXT PRIMARY KEY, teacher_user_id INTEGER, name TEXT NOT NULL,
  class_period TEXT, join_code TEXT UNIQUE NOT NULL, is_active INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
  FOREIGN KEY(teacher_user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS class_enrollments (
  class_id TEXT NOT NULL, student_id TEXT NOT NULL, enrolled_at INTEGER NOT NULL,
  enrolled_by TEXT NOT NULL DEFAULT 'self', is_active INTEGER NOT NULL DEFAULT 1,
  archived_at INTEGER, archived_by_user_id INTEGER, archive_reason TEXT,
  reactivated_at INTEGER, reactivated_by_user_id INTEGER,
  PRIMARY KEY(class_id,student_id),
  FOREIGN KEY(class_id) REFERENCES class_sections(class_id) ON DELETE CASCADE,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(archived_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
  FOREIGN KEY(reactivated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_class_enrollments_student ON class_enrollments(student_id);
CREATE TABLE IF NOT EXISTS assignments (
  assignment_id TEXT PRIMARY KEY, teacher_user_id INTEGER NOT NULL,
  class_id TEXT NOT NULL, target_type TEXT NOT NULL CHECK(target_type IN ('standard','objective','competency')),
  target_id TEXT NOT NULL, recipient_scope TEXT NOT NULL CHECK(recipient_scope IN ('class','selected')),
  directions TEXT, assign_at INTEGER NOT NULL, due_at INTEGER,
  created_at INTEGER NOT NULL, archived_at INTEGER, archived_by INTEGER,
  FOREIGN KEY(teacher_user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(class_id) REFERENCES class_sections(class_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_assignments_teacher ON assignments(teacher_user_id,archived_at,created_at);
CREATE INDEX IF NOT EXISTS idx_assignments_class ON assignments(class_id,archived_at,created_at);
CREATE TABLE IF NOT EXISTS assignment_recipients (
  assignment_id TEXT NOT NULL, student_id TEXT NOT NULL, assigned_at INTEGER NOT NULL,
  PRIMARY KEY(assignment_id,student_id),
  FOREIGN KEY(assignment_id) REFERENCES assignments(assignment_id) ON DELETE RESTRICT,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_assignment_recipients_student
  ON assignment_recipients(student_id,assignment_id);

CREATE TABLE IF NOT EXISTS attempts (
  attempt_id TEXT PRIMARY KEY, student_id TEXT NOT NULL, question_id TEXT NOT NULL,
  timestamp INTEGER NOT NULL, response TEXT, is_correct INTEGER NOT NULL,
  time_seconds REAL, skills_missed TEXT,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(question_id) REFERENCES questions(question_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_attempts_student_obj_ts
  ON attempts(student_id,question_id,timestamp DESC);
CREATE TABLE IF NOT EXISTS progress_state (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL, standard_id TEXT NOT NULL,
  current_level INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'practicing',
  rolling_avg REAL NOT NULL DEFAULT 0.0, locked INTEGER NOT NULL DEFAULT 0,
  locked_reason TEXT, last_update INTEGER NOT NULL,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_progress_state_unique ON progress_state(student_id,standard_id);
CREATE TABLE IF NOT EXISTS routing_level_attempts (
  attempt_id TEXT PRIMARY KEY, student_id TEXT NOT NULL, standard_id TEXT NOT NULL,
  objective_id TEXT NOT NULL, level INTEGER NOT NULL CHECK(level BETWEEN 1 AND 3),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed','invalidated')),
  started_at INTEGER NOT NULL, ended_at INTEGER, end_reason TEXT,
  final_correct_count INTEGER, final_response_count INTEGER, final_score REAL,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_routing_level_attempt_active_student
  ON routing_level_attempts(student_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS responses (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL, standard_id TEXT NOT NULL,
  level INTEGER NOT NULL DEFAULT 1, question_id TEXT NOT NULL,
  correct INTEGER NOT NULL, ts INTEGER NOT NULL, routing_level_attempt_id TEXT,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY(question_id) REFERENCES questions(question_id) ON DELETE CASCADE,
  FOREIGN KEY(routing_level_attempt_id) REFERENCES routing_level_attempts(attempt_id)
);
CREATE INDEX IF NOT EXISTS idx_responses_student_std_lvl_ts
  ON responses(student_id,standard_id,level,ts DESC);
CREATE INDEX IF NOT EXISTS idx_responses_routing_level_attempt
  ON responses(routing_level_attempt_id,id);
CREATE TABLE IF NOT EXISTS student_objective_state (
  student_id TEXT NOT NULL, standard_id TEXT NOT NULL,
  current_objective_id TEXT NOT NULL, status TEXT DEFAULT 'active', last_update INTEGER,
  PRIMARY KEY(student_id,standard_id),
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY(current_objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS student_growth_progress (
  attempt_id TEXT PRIMARY KEY, student_id TEXT NOT NULL, standard_id TEXT NOT NULL,
  objective_id TEXT NOT NULL, visible_stage INTEGER NOT NULL DEFAULT 1 CHECK(visible_stage BETWEEN 1 AND 4),
  presentation_state TEXT NOT NULL DEFAULT 'strengthening' CHECK(presentation_state IN ('growing','strengthening','reviewing','complete')),
  is_active INTEGER NOT NULL DEFAULT 1, started_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL, completed_at INTEGER,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_student_growth_active
  ON student_growth_progress(student_id) WHERE is_active=1;
CREATE TABLE IF NOT EXISTS student_question_deliveries (
  submission_token TEXT PRIMARY KEY, student_id TEXT NOT NULL,
  growth_attempt_id TEXT NOT NULL, standard_id TEXT NOT NULL,
  objective_id TEXT NOT NULL, level INTEGER NOT NULL, question_id TEXT NOT NULL,
  sequence_number INTEGER NOT NULL, served_at INTEGER NOT NULL,
  consumed_at INTEGER, invalidated_at INTEGER,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(growth_attempt_id) REFERENCES student_growth_progress(attempt_id) ON DELETE CASCADE,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE,
  FOREIGN KEY(question_id) REFERENCES questions(question_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_student_question_delivery_active
  ON student_question_deliveries(student_id) WHERE consumed_at IS NULL AND invalidated_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_student_question_delivery_sequence
  ON student_question_deliveries(growth_attempt_id,level,sequence_number);

CREATE TABLE IF NOT EXISTS lists (
  objective_id TEXT PRIMARY KEY, advance_to TEXT, remediation_to TEXT,
  FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS paths (
  id INTEGER PRIMARY KEY AUTOINCREMENT, from_node TEXT NOT NULL, to_node TEXT NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS progression_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT, core_idea TEXT NOT NULL,
  from_band TEXT NOT NULL, to_band TEXT NOT NULL, promote_threshold REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS remediation_links (
  id INTEGER PRIMARY KEY, standard_id TEXT NOT NULL, lower_standard_id TEXT NOT NULL,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY(lower_standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS progression_links (
  id INTEGER PRIMARY KEY, standard_id TEXT NOT NULL, next_standard_id TEXT NOT NULL,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY(next_standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS standard_links (
  id INTEGER PRIMARY KEY AUTOINCREMENT, from_standard_id TEXT NOT NULL,
  to_standard_id TEXT NOT NULL, link_type TEXT NOT NULL, source TEXT,
  priority INTEGER DEFAULT 1, notes TEXT, created_at INTEGER,
  UNIQUE(from_standard_id,to_standard_id,link_type)
);
CREATE TABLE IF NOT EXISTS origin_links (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL, rem_standard_id TEXT NOT NULL,
  from_standard_id TEXT NOT NULL, from_level INTEGER NOT NULL, ts INTEGER NOT NULL,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(rem_standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY(from_standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY, student_id TEXT NOT NULL, standard_id TEXT NOT NULL,
  event TEXT NOT NULL, details TEXT NOT NULL, ts INTEGER NOT NULL,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS frustration_events (
  event_id TEXT PRIMARY KEY, student_id TEXT NOT NULL, objective_id TEXT NOT NULL,
  timestamp INTEGER NOT NULL, trigger TEXT NOT NULL,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE CASCADE,
  FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS question_flags (
  flag_id TEXT PRIMARY KEY, question_id TEXT NOT NULL, objective_id TEXT NOT NULL,
  standard_id TEXT NOT NULL, reporter_user_id INTEGER,
  reporter_role TEXT NOT NULL CHECK(reporter_role IN ('teacher','student')),
  class_id TEXT, student_id TEXT, category TEXT NOT NULL, comment TEXT,
  page_context TEXT, created_ts INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','teacher_resolved','escalated','owner_reviewing','fixed','closed')),
  escalated_by_user_id INTEGER, escalated_at INTEGER, escalation_note TEXT,
  owner_reviewing_at INTEGER, owner_reviewing_by_user_id INTEGER,
  resolved_ts INTEGER, resolution_note TEXT, resolved_by_user_id INTEGER,
  FOREIGN KEY(question_id) REFERENCES questions(question_id) ON DELETE CASCADE,
  FOREIGN KEY(objective_id) REFERENCES objectives(objective_id) ON DELETE CASCADE,
  FOREIGN KEY(standard_id) REFERENCES standards(standard_id) ON DELETE CASCADE,
  FOREIGN KEY(class_id) REFERENCES class_sections(class_id) ON DELETE SET NULL,
  FOREIGN KEY(student_id) REFERENCES students(student_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_question_flags_status_created ON question_flags(status,created_ts DESC);
CREATE INDEX IF NOT EXISTS idx_question_flags_question_status ON question_flags(question_id,status);
CREATE TABLE IF NOT EXISTS question_flag_events (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT, flag_id TEXT NOT NULL, from_status TEXT,
  to_status TEXT NOT NULL CHECK(to_status IN ('open','teacher_resolved','escalated','owner_reviewing','fixed','closed')),
  actor_user_id INTEGER NOT NULL, actor_authority TEXT NOT NULL, note TEXT,
  created_ts INTEGER NOT NULL,
  FOREIGN KEY(flag_id) REFERENCES question_flags(flag_id) ON DELETE CASCADE,
  FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_question_flag_events_flag_created
  ON question_flag_events(flag_id,created_ts,event_id);

CREATE TABLE IF NOT EXISTS access_authorization_audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, actor_user_id INTEGER NOT NULL,
  target_user_id INTEGER NOT NULL, action TEXT NOT NULL CHECK(action IN ('teacher_authorized')),
  authorization_id INTEGER, outcome TEXT NOT NULL CHECK(outcome IN ('granted','already_granted')),
  created_at INTEGER NOT NULL, request_ip TEXT, user_agent TEXT,
  FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(target_user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(authorization_id) REFERENCES user_instructional_authorizations(authorization_id) ON DELETE RESTRICT
);
CREATE INDEX IF NOT EXISTS idx_access_authorization_audit_target
  ON access_authorization_audit_log(target_user_id,created_at,audit_id);
CREATE TABLE IF NOT EXISTS authorization_lifecycle_audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, actor_user_id INTEGER NOT NULL,
  target_user_id INTEGER NOT NULL, action TEXT NOT NULL CHECK(action IN ('teacher_revoked')),
  authorization_id INTEGER, outcome TEXT NOT NULL CHECK(outcome IN ('revoked','already_revoked','blocked_active_classes')),
  reason TEXT, created_at INTEGER NOT NULL,
  FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(target_user_id) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS account_lifecycle_audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, actor_user_id INTEGER NOT NULL,
  target_user_id INTEGER NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('account_deactivated','account_reactivated')),
  outcome TEXT NOT NULL CHECK(outcome IN ('deactivated','reactivated','already_deactivated','already_active','blocked_last_owner')),
  reason TEXT, created_at INTEGER NOT NULL,
  FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(target_user_id) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS class_membership_audit_log (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, actor_user_id INTEGER NOT NULL,
  class_id TEXT NOT NULL, student_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('membership_archived')),
  outcome TEXT NOT NULL CHECK(outcome IN ('archived','already_archived')),
  reason TEXT, created_at INTEGER NOT NULL,
  FOREIGN KEY(actor_user_id) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS impersonation_audit (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, correlation_id TEXT NOT NULL UNIQUE,
  acting_owner_user_id INTEGER NOT NULL, target_user_id INTEGER NOT NULL,
  target_role TEXT NOT NULL, start_timestamp INTEGER NOT NULL, stop_timestamp INTEGER,
  source_ip TEXT, outcome TEXT NOT NULL DEFAULT 'active', termination_reason TEXT,
  FOREIGN KEY(acting_owner_user_id) REFERENCES users(id) ON DELETE RESTRICT,
  FOREIGN KEY(target_user_id) REFERENCES users(id) ON DELETE RESTRICT
);
CREATE TABLE IF NOT EXISTS impersonated_action_audit (
  action_audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
  impersonation_audit_id INTEGER NOT NULL, acting_owner_user_id INTEGER NOT NULL,
  effective_user_id INTEGER NOT NULL, request_method TEXT NOT NULL,
  request_path TEXT NOT NULL, response_status INTEGER NOT NULL, created_at INTEGER NOT NULL,
  FOREIGN KEY(impersonation_audit_id) REFERENCES impersonation_audit(audit_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS diagnostic_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT, question_id TEXT NOT NULL, domain TEXT NOT NULL,
  FOREIGN KEY(question_id) REFERENCES questions(question_id)
);
CREATE TABLE IF NOT EXISTS diagnostic_sessions (
  id TEXT PRIMARY KEY, student_id TEXT NOT NULL, started_at INTEGER NOT NULL,
  completed_at INTEGER, status TEXT NOT NULL,
  FOREIGN KEY(student_id) REFERENCES students(student_id)
);
CREATE TABLE IF NOT EXISTS diagnostic_responses (
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
  diagnostic_item_id INTEGER NOT NULL, is_correct INTEGER,
  FOREIGN KEY(session_id) REFERENCES diagnostic_sessions(id),
  FOREIGN KEY(diagnostic_item_id) REFERENCES diagnostic_items(id)
);
CREATE TABLE IF NOT EXISTS error_logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, level TEXT NOT NULL,
  path TEXT, user_id TEXT, role TEXT, message TEXT NOT NULL, traceback TEXT
);
