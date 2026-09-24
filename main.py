import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, Date, DateTime, ForeignKey, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DB_URL = os.getenv("DATABASE_URL", "sqlite:///./leads.db")
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()

STATUSES = ["New", "Contacted", "Interested", "Application Started", "Admitted", "Lost"]
# Allowed transitions. Lost leads can be reopened as Contacted. Admitted is final.
TRANSITIONS = {
    "New": ["Contacted", "Lost"],
    "Contacted": ["Interested", "Lost"],
    "Interested": ["Application Started", "Lost"],
    "Application Started": ["Admitted", "Lost"],
    "Admitted": [],
    "Lost": ["Contacted"],
}
CLOSED = ("Admitted", "Lost")
SOURCES = ["Website", "Walk-in", "Phone", "WhatsApp", "Fair", "Campaign", "Other"]
SOURCE_WEIGHT = {"Walk-in": 20, "Phone": 15, "Website": 10, "WhatsApp": 10, "Fair": 10, "Campaign": 5, "Other": 5}
STAGE_SCORE = {"New": 10, "Contacted": 25, "Interested": 45, "Application Started": 65}
STALE_DAYS = 3


class Counsellor(Base):
    __tablename__ = "counsellors"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    active = Column(Boolean, default=True)


class Lead(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False)
    phone_key = Column(String, index=True)  # last 10 digits, used for duplicate detection
    email = Column(String, index=True)
    source = Column(String, nullable=False)
    course = Column(String, nullable=False)
    status = Column(String, default="New")
    counsellor_id = Column(Integer, ForeignKey("counsellors.id"))
    next_followup = Column(Date)
    lost_reason = Column(String)
    created_at = Column(DateTime, default=datetime.now)
    last_contact_at = Column(DateTime, default=datetime.now)


class Activity(Base):
    __tablename__ = "activities"
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), index=True)
    kind = Column(String)
    note = Column(Text)
    created_at = Column(DateTime, default=datetime.now)


def log(db, lead_id, kind, note):
    db.add(Activity(lead_id=lead_id, kind=kind, note=note, created_at=datetime.now()))


def phone_key(p: str) -> str:
    return re.sub(r"\D", "", p or "")[-10:]


def pick_counsellor(db, exclude_id=None):
    """Assign to the active counsellor with the fewest open leads (ties -> lowest id)."""
    best, best_n = None, None
    for c in db.query(Counsellor).filter(Counsellor.active == True).order_by(Counsellor.id):  # noqa: E712
        if c.id == exclude_id:
            continue
        n = db.query(Lead).filter(Lead.counsellor_id == c.id, Lead.status.notin_(CLOSED)).count()
        if best is None or n < best_n:
            best, best_n = c, n
    return best


def flags(l):
    open_ = l.status not in CLOSED
    age = (datetime.now() - (l.last_contact_at or l.created_at)).days
    overdue = bool(open_ and l.next_followup and l.next_followup < date.today())
    stale = bool(open_ and age >= STALE_DAYS)
    return open_, age, overdue, stale


def score(l):
    open_, age, overdue, stale = flags(l)
    if not open_:
        return 0, "Closed lead"
    s = STAGE_SCORE[l.status]
    why = [f"stage '{l.status}' (+{s})"]
    w = SOURCE_WEIGHT.get(l.source, 5)
    s += w
    why.append(f"{l.source} source (+{w})")
    if overdue:
        s -= 15
        why.append("follow-up overdue (-15)")
    if stale:
        s -= 10
        why.append(f"no contact for {age} days (-10)")
    return max(0, min(100, s)), ", ".join(why)


def suggest_message(l, cname):
    who = cname or "the admissions team"
    m = {
        "New": f"Hi {l.name}, thanks for your interest in {l.course}. I'm {who} from admissions. When is a good time for a quick call?",
        "Contacted": f"Hi {l.name}, following up on our chat about {l.course}. Shall I share the fee structure and eligibility details?",
        "Interested": f"Hi {l.name}, ready to take the next step for {l.course}? The application takes about 10 minutes and I can walk you through it.",
        "Application Started": f"Hi {l.name}, I see your {l.course} application is in progress. Can I help with any documents or questions?",
        "Lost": f"Hi {l.name}, we'd love to help you again with {l.course} if your plans have changed. Happy to answer any questions.",
        "Admitted": f"Hi {l.name}, congratulations on your admission to {l.course}! Let me know if you need anything for onboarding.",
    }
    return m[l.status]


def lead_dict(db, l):
    open_, age, overdue, stale = flags(l)
    sc, why = score(l)
    c = db.get(Counsellor, l.counsellor_id) if l.counsellor_id else None
    return {
        "id": l.id, "name": l.name, "phone": l.phone, "email": l.email, "source": l.source,
        "course": l.course, "status": l.status, "counsellor_id": l.counsellor_id,
        "counsellor": c.name if c else None,
        "next_followup": l.next_followup.isoformat() if l.next_followup else None,
        "lost_reason": l.lost_reason, "created_at": l.created_at.isoformat(),
        "age_days": age, "overdue": overdue, "stale": stale,
        "score": sc, "score_reason": why,
        "priority": "High" if sc >= 60 else "Medium" if sc >= 35 else "Low",
        "allowed": TRANSITIONS[l.status],
    }


class LeadIn(BaseModel):
    name: str
    phone: str
    email: Optional[str] = None
    source: str
    course: str


class StatusIn(BaseModel):
    status: str
    next_followup: Optional[date] = None
    note: Optional[str] = None
    lost_reason: Optional[str] = None


class NoteIn(BaseModel):
    note: str
    next_followup: Optional[date] = None


class ReassignIn(BaseModel):
    counsellor_id: int


class CounsellorIn(BaseModel):
    name: str


def seed(db):
    for n in ["Asha", "Ravi", "Meera"]:
        db.add(Counsellor(name=n))
    db.commit()
    today = datetime.now()
    rows = [
        ("Ankit Sharma", "9876500001", "Website", "B.Tech CSE", "Interested", 2, 1),
        ("Priya Nair", "9876500002", "Walk-in", "MBA", "Application Started", 1, 2),
        ("Rahul Verma", "9876500003", "Phone", "B.Com", "Contacted", 6, -2),
        ("Sneha Rao", "9876500004", "WhatsApp", "B.Tech ECE", "New", 0, None),
        ("Karthik S", "9876500005", "Fair", "BBA", "Admitted", 4, None),
        ("Divya M", "9876500006", "Campaign", "MBA", "Lost", 9, None),
        ("Imran Khan", "9876500007", "Website", "B.Sc", "Interested", 5, -1),
        ("Neha Gupta", "9876500008", "Walk-in", "B.Tech CSE", "Contacted", 1, 3),
        ("Vikram Singh", "9876500009", "Phone", "BCA", "New", 4, None),
        ("Anjali Das", "9876500010", "Website", "MBA", "Admitted", 7, None),
        ("Suresh Babu", "9876500011", "Campaign", "B.Com", "Lost", 12, None),
        ("Pooja Iyer", "9876500012", "WhatsApp", "BBA", "Application Started", 3, -1),
    ]
    for i, (n, p, s, c, st, ago, fu) in enumerate(rows):
        lead = Lead(
            name=n, phone=p, phone_key=phone_key(p), email=f"{n.split()[0].lower()}@example.com",
            source=s, course=c, status=st, counsellor_id=(i % 3) + 1,
            created_at=today - timedelta(days=ago + 2), last_contact_at=today - timedelta(days=ago),
            next_followup=(date.today() + timedelta(days=fu)) if fu is not None else None,
            lost_reason="Chose another college" if st == "Lost" else None,
        )
        db.add(lead)
        db.flush()
        log(db, lead.id, "created", f"Lead created from {s}")
        if st != "New":
            log(db, lead.id, "status", f"Status set to {st}")
    db.commit()


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    if os.getenv("SEED", "1") == "1":
        with SessionLocal() as db:
            if db.query(Counsellor).count() == 0:
                seed(db)
    yield


app = FastAPI(title="Admission Lead Manager", lifespan=lifespan)


@app.get("/api/meta")
def meta():
    return {"statuses": STATUSES, "sources": SOURCES, "stale_days": STALE_DAYS}


@app.get("/api/counsellors")
def counsellors():
    with SessionLocal() as db:
        out = []
        for c in db.query(Counsellor).filter(Counsellor.active == True).order_by(Counsellor.id):  # noqa: E712
            open_n = db.query(Lead).filter(Lead.counsellor_id == c.id, Lead.status.notin_(CLOSED)).count()
            out.append({"id": c.id, "name": c.name, "open_leads": open_n})
        return out


@app.post("/api/counsellors")
def add_counsellor(body: CounsellorIn):
    if not body.name.strip():
        raise HTTPException(400, "Name is required")
    with SessionLocal() as db:
        c = Counsellor(name=body.name.strip())
        db.add(c)
        db.commit()
        return {"id": c.id, "name": c.name}


@app.delete("/api/counsellors/{cid}")
def remove_counsellor(cid: int):
    """Deactivates a counsellor and redistributes their open leads to the remaining ones."""
    with SessionLocal() as db:
        c = db.get(Counsellor, cid)
        if not c or not c.active:
            raise HTTPException(404, "Counsellor not found")
        others = db.query(Counsellor).filter(Counsellor.active == True, Counsellor.id != cid).count()  # noqa: E712
        open_leads = db.query(Lead).filter(Lead.counsellor_id == cid, Lead.status.notin_(CLOSED)).all()
        if open_leads and others == 0:
            raise HTTPException(400, "Cannot remove the only counsellor while they own open leads")
        c.active = False
        db.flush()
        moved = 0
        for l in open_leads:
            new = pick_counsellor(db, exclude_id=cid)
            l.counsellor_id = new.id
            log(db, l.id, "reassigned", f"{c.name} removed; auto-reassigned to {new.name}")
            db.flush()
            moved += 1
        db.commit()
        return {"removed": c.name, "leads_reassigned": moved}


@app.post("/api/leads")
def create_lead(body: LeadIn):
    name, course = body.name.strip(), body.course.strip()
    if not name or not course:
        raise HTTPException(400, "Name and course are required")
    if body.source not in SOURCES:
        raise HTTPException(400, f"Source must be one of {SOURCES}")
    key = phone_key(body.phone)
    if len(key) < 10:
        raise HTTPException(400, "Phone must have at least 10 digits")
    email = (body.email or "").strip().lower() or None
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(400, "Invalid email")
    with SessionLocal() as db:
        dup = db.query(Lead).filter(Lead.phone_key == key).first()
        if not dup and email:
            dup = db.query(Lead).filter(Lead.email == email).first()
        if dup:
            log(db, dup.id, "duplicate", f"Repeat enquiry via {body.source} for {course}")
            dup.last_contact_at = datetime.now()
            db.commit()
            d = lead_dict(db, dup)
            d["duplicate"] = True
            return d
        c = pick_counsellor(db)
        lead = Lead(name=name, phone=body.phone.strip(), phone_key=key, email=email, source=body.source,
                    course=course, counsellor_id=c.id if c else None)
        db.add(lead)
        db.flush()
        log(db, lead.id, "created", f"Lead created from {body.source}" + (f", assigned to {c.name}" if c else ", unassigned (no counsellors)"))
        db.commit()
        d = lead_dict(db, lead)
        d["duplicate"] = False
        return d


@app.get("/api/leads")
def list_leads(status: Optional[str] = None, counsellor_id: Optional[int] = None,
               overdue: bool = False, q: Optional[str] = None):
    with SessionLocal() as db:
        qs = db.query(Lead)
        if status:
            qs = qs.filter(Lead.status == status)
        if counsellor_id:
            qs = qs.filter(Lead.counsellor_id == counsellor_id)
        if q:
            like = f"%{q.strip()}%"
            qs = qs.filter(Lead.name.ilike(like) | Lead.phone.ilike(like))
        out = [lead_dict(db, l) for l in qs.all()]
        if overdue:
            out = [d for d in out if d["overdue"]]
        out.sort(key=lambda d: (d["status"] in CLOSED, -d["score"]))
        return out


@app.get("/api/leads/{lid}")
def get_lead(lid: int):
    with SessionLocal() as db:
        l = db.get(Lead, lid)
        if not l:
            raise HTTPException(404, "Lead not found")
        d = lead_dict(db, l)
        d["suggested_message"] = suggest_message(l, d["counsellor"])
        acts = db.query(Activity).filter(Activity.lead_id == lid).order_by(Activity.id.desc()).all()
        d["activities"] = [{"kind": a.kind, "note": a.note, "at": a.created_at.isoformat()} for a in acts]
        return d


@app.post("/api/leads/{lid}/status")
def change_status(lid: int, body: StatusIn):
    with SessionLocal() as db:
        l = db.get(Lead, lid)
        if not l:
            raise HTTPException(404, "Lead not found")
        if body.status not in STATUSES:
            raise HTTPException(400, "Unknown status")
        if body.status not in TRANSITIONS[l.status]:
            raise HTTPException(400, f"Invalid move: {l.status} -> {body.status}. Allowed: {TRANSITIONS[l.status] or 'none (final)'}")
        if body.status == "Lost":
            if not (body.lost_reason or "").strip():
                raise HTTPException(400, "A reason is required to mark a lead as Lost")
            l.lost_reason = body.lost_reason.strip()
            l.next_followup = None
        elif body.status == "Admitted":
            l.next_followup = None
            l.lost_reason = None
        else:
            if not body.next_followup:
                raise HTTPException(400, "Next follow-up date is required")
            if body.next_followup < date.today():
                raise HTTPException(400, "Follow-up date cannot be in the past")
            l.next_followup = body.next_followup
            l.lost_reason = None
        old = l.status
        l.status = body.status
        l.last_contact_at = datetime.now()
        log(db, l.id, "status", f"{old} -> {body.status}" + (f" ({l.lost_reason})" if body.status == "Lost" else "") + (f". {body.note}" if body.note else ""))
        db.commit()
        return lead_dict(db, l)


@app.post("/api/leads/{lid}/note")
def add_note(lid: int, body: NoteIn):
    with SessionLocal() as db:
        l = db.get(Lead, lid)
        if not l:
            raise HTTPException(404, "Lead not found")
        if not body.note.strip():
            raise HTTPException(400, "Note is empty")
        if body.next_followup:
            if l.status in CLOSED:
                raise HTTPException(400, "Closed leads cannot have a follow-up")
            if body.next_followup < date.today():
                raise HTTPException(400, "Follow-up date cannot be in the past")
            l.next_followup = body.next_followup
        l.last_contact_at = datetime.now()
        log(db, l.id, "note", body.note.strip() + (f" (next follow-up {body.next_followup})" if body.next_followup else ""))
        db.commit()
        return lead_dict(db, l)


@app.post("/api/leads/{lid}/reassign")
def reassign(lid: int, body: ReassignIn):
    with SessionLocal() as db:
        l = db.get(Lead, lid)
        c = db.get(Counsellor, body.counsellor_id)
        if not l:
            raise HTTPException(404, "Lead not found")
        if not c or not c.active:
            raise HTTPException(400, "Counsellor not found or inactive")
        old = db.get(Counsellor, l.counsellor_id) if l.counsellor_id else None
        if old and old.id == c.id:
            raise HTTPException(400, "Lead is already assigned to this counsellor")
        l.counsellor_id = c.id
        log(db, l.id, "reassigned", f"{old.name if old else 'Unassigned'} -> {c.name}")
        db.commit()
        return lead_dict(db, l)


@app.get("/api/dashboard")
def dashboard():
    with SessionLocal() as db:
        leads = [lead_dict(db, l) for l in db.query(Lead).all()]
        funnel = {s: 0 for s in STATUSES}
        by_source = {}
        by_c = {}
        for d in leads:
            funnel[d["status"]] += 1
            s = by_source.setdefault(d["source"], {"source": d["source"], "total": 0, "admitted": 0})
            s["total"] += 1
            s["admitted"] += d["status"] == "Admitted"
            if d["status"] not in CLOSED:
                c = by_c.setdefault(d["counsellor"] or "Unassigned", {"counsellor": d["counsellor"] or "Unassigned", "open": 0, "overdue": 0, "stale": 0})
                c["open"] += 1
                c["overdue"] += d["overdue"]
                c["stale"] += d["stale"]
        for s in by_source.values():
            s["conversion_pct"] = round(100 * s["admitted"] / s["total"], 1) if s["total"] else 0
        total = len(leads)
        return {
            "total": total,
            "open": sum(d["status"] not in CLOSED for d in leads),
            "overdue": sum(d["overdue"] for d in leads),
            "stale": sum(d["stale"] for d in leads),
            "admitted": funnel["Admitted"],
            "conversion_pct": round(100 * funnel["Admitted"] / total, 1) if total else 0,
            "funnel": funnel,
            "by_source": sorted(by_source.values(), key=lambda x: -x["total"]),
            "by_counsellor": sorted(by_c.values(), key=lambda x: -x["open"]),
            "lost_reasons": _lost_reasons(leads),
        }


def _lost_reasons(leads):
    r = {}
    for d in leads:
        if d["status"] == "Lost":
            r[d["lost_reason"] or "Unknown"] = r.get(d["lost_reason"] or "Unknown", 0) + 1
    return r


@app.get("/")
def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "static", "index.html"))
