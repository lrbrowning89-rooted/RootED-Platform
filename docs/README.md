# Adaptive NGSS Science Platform — Phase 1 Setup

Version v0.5 – Rolling-7 Engine Active  
Author: Logan Browning  
Location: Home and local coffee shops ☕

---

## 🧭 Purpose
This README serves as project documentation for setup, structure, and maintenance.  
It ensures the Adaptive NGSS Science Platform can be easily understood, updated, or deployed on a new computer without confusion.

---

## 🗂 Folder Structure
NGSS_Project/
├── app_core/ ← Flask app & adaptive engine code
├── data/ ← SQLite DB, backups, imports/exports
├── migrations/ ← Schema & maintenance scripts
├── docs/ ← Project notes and README
├── static/ ← Future CSS/JS/images
├── templates/ ← Future Jinja HTML templates
└── tests/ ← Unit tests

yaml
Copy code

---

## ⚙️ Quick Start
```bash
cd app_core
python dashboard_v5.py
Then open the browser link (usually http://127.0.0.1:5000/).

Run engine test:

bash
Copy code
cd tests
python test_engine.py
Expected output:

bash
Copy code
✅ Rolling-7 test passed, avg≈0.57
All tests passed!
🧩 Core Files
File	Purpose
dashboard_v5.py	Teacher dashboard / student practice routes
adaptive_engine.py	Rolling-7 mastery logic
schema.sql	Database structure (master copy)
config.py	Security + FERPA settings
migrate_schema.py	Creates/upgrades database schema
test_engine.py	Basic engine sanity test

🛡️ FERPA & Data Safety
Names masked by default

Local SQLite DB only (stays on device)

Manual backups via Maintenance panel

🧠 Next Steps
Add test_dashboard.py for route testing

Build analytics panels (Phase 2)

Add mini-lessons + remediation (Phase 3)

Automate cloud backups (Phase 4)

“Build boldly. Teach creatively. Let the data guide the discovery.”
— RootED Vision Statement (Draft)