# Admission Lead Manager

A working prototype for the Edumerge Pre-Drive assignment (Assignment 5: Admission Lead Management).
Manages a lead from first contact to admission, with counsellor assignment, follow-ups, ageing, and a manager dashboard.

**Live demo:** <https://lead-manager-6cl3.onrender.com> (seeded with demo data)

## Run locally (Windows CMD)
```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --reload
```
Open http://127.0.0.1:8000. Demo data (3 counsellors, 12 leads) loads on first start. API docs: `/docs`.
Run tests: `python -m pytest -q`

## Users
- **Counsellor**: captures leads, updates status, adds notes, works their follow-up list.
- **Manager**: dashboard for funnel, source conversion, workload, overdue and stale leads, and reasons for losses.
- No login in this prototype (see trade-offs).

## Features
- One capture form for all sources (Website, Walk-in, Phone, WhatsApp, Fair, Campaign, Other) with course preference.
- Lifecycle: New -> Contacted -> Interested -> Application Started -> Admitted, or Lost from any open stage.
- Auto-assignment to the active counsellor with the fewest open leads; manual reassignment; all changes logged.
- Follow-up date is mandatory on every forward move; overdue list and filter.
- Ageing: days since last contact; a lead is "stale" after 3 days without contact.
- Activity timeline per lead (creation, status changes, notes, reassignments, duplicate enquiries).
- Manager dashboard with funnel, conversion by source, counsellor workload, and loss reasons.
- Lead priority score (0-100) with a written reason, plus a suggested follow-up message.

## Assumptions
1. A person is identified by phone number (last 10 digits, so "+91 98765-11111" equals "9876511111") or by email.
2. A repeat enquiry is not a new lead. It is logged on the existing lead's timeline and counts as recent contact.
3. Admitted is final. A Lost lead can be reopened, but only to Contacted, with a new follow-up date.
4. "Lost" requires a reason so that management can see why leads are dropping.
5. Leads are never deleted, only closed, to preserve history and conversion numbers.
6. Stale threshold is 3 days (`STALE_DAYS` in `main.py`).

## Edge cases handled (covered by `test_app.py`)
- Duplicate lead by phone (different formatting) or by email (different case).
- Invalid status jump (e.g. New -> Admitted) rejected with the list of allowed moves.
- Lost without a reason rejected.
- Missing follow-up date or a date in the past rejected.
- Nothing can follow Admitted.
- Invalid phone (<10 digits) or email rejected.
- Removing a counsellor auto-redistributes their open leads; blocked if they are the only counsellor and still own open leads.
- Lead created when no counsellor exists stays unassigned instead of crashing.

## Architecture
FastAPI + SQLAlchemy + SQLite, with a single-page vanilla JS frontend served by the same app. Three tables: `counsellors`, `leads`, `activities`.
Business rules (transition map, validation) live in the backend so the UI cannot bypass them.

## Trade-offs
- **SQLite** keeps setup to zero and is fine for a prototype. For production, switch to Postgres by changing `DATABASE_URL`. On free hosting the file resets on redeploy, and demo data is re-seeded.
- **Least-loaded assignment** instead of strict round-robin: it stays balanced after reassignments and removals.
- **Priority score is a transparent rule-based heuristic** (stage + source weight, minus penalties for overdue/stale), not a trained model. It is explainable and needs no API key. Real conversion data would allow a trained model.
- **No authentication** to keep scope tight. The next step would be roles (counsellor sees only own leads, manager sees all).
- Vanilla JS rather than a framework: fewer moving parts and a faster build.

## What I would build next
Login and roles, WhatsApp/email integration for messages, bulk CSV import, follow-up reminders/notifications, an LLM-generated message personalised to lead history, and per-counsellor targets.
