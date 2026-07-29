# Platform roles, tool boundaries, and impersonation

## Responsibilities

Teachers manage classroom work: classes, join codes, rosters, assignments,
student progress and support signals, question preview, Student Mode preview,
and classroom-scoped question flags.

Owners manage accounts and authorization, impersonation, content imports,
global configuration, backups and restores, destructive maintenance, database
status, error logs, engine diagnostics, routing calculations, and artificial
test operations. Owners can also open Teacher View to verify classroom flows.

## Dashboard classification

| Existing capability | Classification and disposition |
| --- | --- |
| Student Mode | Teacher classroom tool; retained as a safe preview |
| Adaptive Assessment Simulator (legacy `/diagnostic` routes) | Developer/testing tool; moved to Owner diagnostics because it creates artificial diagnostic sessions and responses |
| View Error Log | Developer/debug; Owner-only |
| Question Flags | Teacher classroom support plus Owner escalation review; retained in both scoped workflows |
| Class Snapshot, Needs Attention, Active Standards, Not Started, Progressing/Mastered | Teacher classroom tools; retained |
| Assignments and Question Preview | Teacher classroom tools; retained |
| Class Enrollment, join codes, roster | Teacher classroom tools; retained and class-scoped |
| Record an Attempt and Quick Score | Developer/debug artificial operations; Owner-only |
| Class Aggregate and detailed Class View routing calculations | Developer/debug; Owner-only |
| Configuration thresholds | Owner platform configuration |
| Add / Update Student (`save_student`) | Legacy global roster operation; UI removed, Owner-only compatibility branch retained |
| CSV content import | Owner content administration |
| Backup, restore, clear attempts | Owner data and maintenance |
| Platform version/database details and engine status | Owner maintenance/diagnostics |
| Objective rolling-average and next-node table | Developer/debug; Owner-only |
| Disabled `create_user` dashboard form | Obsolete UI removed; Owner Account Administration is the supported replacement |
| `/user_admin` | Legacy route retained as Owner-only; no teacher UI |

No adaptive-learning rules or routing behavior changed in this phase.

## Owner navigation

The Owner Workspace links to Teacher View, Account Administration (including
People & Access and Impersonate User), Content Administration, Platform
Configuration, Data and Maintenance, and Developer and Diagnostics.

People & Access defaults to active accounts and supports Active, Deactivated,
and All views plus search, canonical teacher, and canonical class filters. No
school filter is shown because RootED has no canonical organization or school
relationship yet. A future model should add organizations, schools, explicit
user affiliations, and school-scoped roles rather than infer school from names.

The role-grant model already supports multiple Owners. Impersonation continues
to reject every Owner target, and existing lifecycle checks protect the final
active Owner. Owner promotion remains deferred until a dedicated, strongly
confirmed and audited grant/revocation workflow is designed; no casual
promotion control is exposed.

Permanent deletion uses anonymization rather than row deletion. It is available
only after deactivation and never for an Owner, the effective current account,
or an impersonated session. Credentials, SSO identities, and personal account
fields are irreversibly removed, while the user ID remains as an anonymous
tombstone so instructional and audit history is preserved without orphaning or
cascade-deleting meaningful records.

## Impersonation security model

Only an authenticated Owner can start impersonation. Start and stop are POST
operations protected by session-bound CSRF tokens. The target must exist and be
active. Owner targets, unsupported targets, and nested impersonation are
rejected.

The ordinary session user becomes the effective target identity. Authorization
therefore uses the target's permissions; no hidden Owner authority is retained.
The original Owner identity is preserved in the server-side
`impersonation_audit` record and the session carries only its audit ID and a
random correlation value. Session state is cleared when switching identities.
The signed, HttpOnly, SameSite session cookie continues to use the application's
normal expiration and secure-cookie configuration.

Every impersonated HTML response has a persistent, non-dismissible banner that
names the effective user, effective role, and acting Owner, and includes the
dedicated Stop Impersonating POST form. Stop works from target pages even when
Owner routes are forbidden. Logout closes the audit and clears impersonation.

The audit stores acting Owner ID, effective target ID and role, timestamps,
source IP, correlation ID, outcome, and termination reason. Non-read requests
during impersonation are recorded in `impersonated_action_audit` with both
identities, method, path, status, and timestamp. Passwords, tokens, and secrets
are not recorded.

## Limitations and deferred work

The audit is deliberately the smallest canonical structure needed for support
impersonation; a platform-wide enterprise audit-log expansion is deferred.
Teacher roster pre-provisioning and identity-claim workflows remain deferred.
Adaptive Assessment Simulator remains an internal test utility. The
**Diagnostic Arena** name is reserved for a future student-facing adaptive
diagnostic spanning standards, grade levels, science domains, prerequisite and
advanced knowledge, and eventually cross-curricular and reading-level evidence.
Designing that product is separate future work.
