import os
import tempfile

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["SEED"] = "0"

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture(scope="module")
def c():
    with TestClient(app) as client:
        client.post("/api/counsellors", json={"name": "A"})
        client.post("/api/counsellors", json={"name": "B"})
        yield client


def new_lead(c, phone, **kw):
    body = {"name": "Test", "phone": phone, "source": "Website", "course": "MBA", **kw}
    return c.post("/api/leads", json=body)


def tomorrow():
    return (date.today() + timedelta(days=1)).isoformat()


def test_duplicate_phone_is_merged_not_created(c):
    a = new_lead(c, "+91 98765-11111").json()
    b = new_lead(c, "9876511111", source="Walk-in").json()
    assert b["duplicate"] is True and b["id"] == a["id"]
    assert len(c.get(f"/api/leads/{a['id']}").json()["activities"]) == 2


def test_duplicate_by_email(c):
    a = new_lead(c, "9876522222", email="x@y.com").json()
    b = new_lead(c, "9876533333", email="X@Y.com").json()
    assert b["duplicate"] is True and b["id"] == a["id"]


def test_invalid_status_jump_blocked(c):
    lid = new_lead(c, "9876544444").json()["id"]
    r = c.post(f"/api/leads/{lid}/status", json={"status": "Admitted"})
    assert r.status_code == 400


def test_lost_requires_reason(c):
    lid = new_lead(c, "9876555555").json()["id"]
    assert c.post(f"/api/leads/{lid}/status", json={"status": "Lost"}).status_code == 400
    ok = c.post(f"/api/leads/{lid}/status", json={"status": "Lost", "lost_reason": "Fees too high"})
    assert ok.status_code == 200 and ok.json()["status"] == "Lost"


def test_followup_required_and_not_in_past(c):
    lid = new_lead(c, "9876566666").json()["id"]
    assert c.post(f"/api/leads/{lid}/status", json={"status": "Contacted"}).status_code == 400
    past = (date.today() - timedelta(days=1)).isoformat()
    assert c.post(f"/api/leads/{lid}/status", json={"status": "Contacted", "next_followup": past}).status_code == 400
    ok = c.post(f"/api/leads/{lid}/status", json={"status": "Contacted", "next_followup": tomorrow()})
    assert ok.status_code == 200


def test_full_happy_path_and_final_state(c):
    lid = new_lead(c, "9876577777").json()["id"]
    for s in ["Contacted", "Interested", "Application Started"]:
        assert c.post(f"/api/leads/{lid}/status", json={"status": s, "next_followup": tomorrow()}).status_code == 200
    assert c.post(f"/api/leads/{lid}/status", json={"status": "Admitted"}).status_code == 200
    assert c.post(f"/api/leads/{lid}/status", json={"status": "Contacted", "next_followup": tomorrow()}).status_code == 400


def test_invalid_phone_rejected(c):
    assert new_lead(c, "123").status_code == 400


def test_remove_counsellor_redistributes_open_leads(c):
    cid = c.post("/api/counsellors", json={"name": "Temp"}).json()["id"]
    for i in range(6):
        new_lead(c, f"98760000{i:02d}")
    owned = [l for l in c.get("/api/leads").json() if l["counsellor_id"] == cid]
    r = c.delete(f"/api/counsellors/{cid}")
    assert r.status_code == 200 and r.json()["leads_reassigned"] == len(owned)
    assert not [l for l in c.get("/api/leads").json() if l["counsellor_id"] == cid and l["status"] not in ("Admitted", "Lost")]


def test_dashboard_shape(c):
    d = c.get("/api/dashboard").json()
    assert d["total"] == sum(d["funnel"].values())
