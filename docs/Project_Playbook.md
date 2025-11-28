# Adaptive NGSS Science Platform — Project Playbook

Author: Logan Browning  
Location: Home and local coffee shops ☕  
Version: v0.5 — Rolling-7 Engine Active

---

## 🧭 Purpose
This playbook tracks the ongoing development of the Adaptive NGSS Science Platform through clear phases and steps.  
Each phase builds on the last to ensure strong architecture, data integrity, and instructional value.  
Use this document to record progress, notes, and “what’s next” at any point in time.

---

## 📈 Phase 1 — Foundation and Core Setup

| Step | Focus | Status | Notes |
|------|--------|---------|-------|
| 1. Environment Setup | Python, Flask, SQLite installed and running | ✅ Done | App runs locally |
| 2. File Organization | Project folder structure cleaned & documented | ✅ Done | All files sorted into app_core, data, migrations, docs, etc. |
| 3. Documentation | README.md and Project_Playbook.md created | ✅ Done | Both stored in /docs |
| 4. Rolling-7 Engine Integration | Adaptive logic fully wired in and tested | ✅ Done | Test script passes |
| 5. Data Import/Export Tools | CSV import/export panels built | ✅ Done | Functioning in dashboard |
| 6. Backup & Restore System | Working backup/restore flow | ✅ Done | Tested manually |
| 7. Unit Tests | Basic engine test created (test_engine.py) | ✅ Done | Passes successfully |
| 8. Schema Migration Tools | migrate_schema.py + repair scripts functional | ⚙️ In Progress | Next testing focus |
| 9. Analytics Base | Placeholder structure for teacher analytics | 🔜 Pending | Phase 2 start point |

---

## 📊 Phase 2 — Analytics & Visualization

| Step | Focus | Status | Notes |
|------|--------|---------|-------|
| 1. Teacher Dashboard Analytics | Summary cards and data charts | 🔜 Pending | Use Matplotlib or Recharts |
| 2. Rolling Performance Graphs | Per-student & per-standard trends | 🔜 Pending | Pull data from attempts + responses |
| 3. Frustration Heatmaps | Identify struggling standards | 🔜 Pending | Use frustration_events table |
| 4. CSV/Excel Reports | Downloadable class reports | 🔜 Pending | May integrate with exports folder |
| 5. Dashboard Filters | Grade, class period, standard filters | 🔜 Pending | Add to Flask routes |

---

## 🧠 Phase 3 — Mini-Lessons & Remediation

| Step | Focus | Status | Notes |
|------|--------|---------|-------|
| 1. Mini-Lesson Database | Store embedded short lessons per standard | 🔜 Pending | Create lessons table |
| 2. Remediation Pathways | Automatic drop & return logic | 🔜 Pending | Tied to lower_band standards |
| 3. Lock/Unlock Events | UI for student lock notifications | 🔜 Pending | Use notifications table |
| 4. Return Logic | After remediation mastery ≥95% | 🔜 Pending | Handled by adaptive_engine |

---

## 🧩 Phase 4 — Cloud, Security, and Scaling

| Step | Focus | Status | Notes |
|------|--------|---------|-------|
| 1. Cloud Sync (Optional) | Cloud-hosted SQLite or PostgreSQL | 🔜 Pending | Optional deployment |
| 2. User Authentication | Teacher login and student dashboard | 🔜 Pending | Flask-Login or JWT |
| 3. HTTPS & Protection | SSL/TLS + data encryption | 🔜 Pending | To be configured in Platform Development chat |
| 4. Multi-Class Management | Multiple class rosters | 🔜 Pending | Tied to teacher accounts |

---

## 🌱 Phase 5 — Curriculum & NGSS Framework

| Step | Focus | Status | Notes |
|------|--------|---------|-------|
| 1. Full MS-LS Framework | Import all standards + objectives | ⚙️ In Progress | Building in Life Science Standards chat |
| 2. MS-PS Framework | Physical Science integration | 🔜 Pending | Next science band |
| 3. MS-ESS Framework | Earth & Space Science integration | 🔜 Pending | After PS complete |
| 4. Elementary Remediation Bands | Lower-grade fallback logic | 🔜 Pending | 3-5 and K-2 standards |
| 5. Data Validation | Ensure all mappings are clean | 🔜 Pending | Check for missing objectives/questions |

---

## 🧭 Phase 6 — User Experience and Extensions

| Step | Focus | Status | Notes |
|------|--------|---------|-------|
| 1. UI Styling | CSS in static folder | 🔜 Pending | Simple Tailwind or Bootstrap |
| 2. Templates Conversion | Move inline HTML → templates folder | 🔜 Pending | Use Jinja includes |
| 3. Student Gamification | Badges, streaks, mastery progress bars | 🔜 Pending | Optional engagement layer |
| 4. Multi-Domain Expansion | Cross-curricular adaptive engine (Math, ELA) | 🔜 Pending | Future milestone |

---

## ✅ Overall Summary

| Category | Progress |
|-----------|-----------|
| Core Engine | ✅ Complete |
| Data Handling | ✅ Complete |
| Testing | ✅ Basic engine test passing |
| Organization | ✅ Clean project structure |
| Analytics | 🔜 Phase 2 target |
| Curriculum Integration | ⚙️ In progress |
| UI / UX | 🔜 Future |
| Cloud Deployment | 🔜 Future |

---

> “Every phase lays a brick. Every test strengthens the foundation.”  
> — *Project Motto*
