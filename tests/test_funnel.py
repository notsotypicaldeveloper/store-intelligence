# PROMPT: Write tests for the session-based conversion funnel endpoint per R12.
#         Funnel: Entry → Zone Visit → Billing Queue → Purchase.
#         REENTRY must collapse to original visitor (not double-count).
#         Staff excluded. Zero-state must not crash.
# CHANGES MADE: Full funnel test coverage with fixture helpers and parametrized integrity check.
import os
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
STORE = "ST1008"


def ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def ingest(events: list[dict]):
    resp = client.post("/events/ingest", json={"events": events})
    assert resp.status_code == 200, resp.text


def entry_event(visitor_id: str, is_staff: bool = False, store_id: str = STORE) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY",
        "visitor_id": visitor_id,
        "event_type": "ENTRY",
        "timestamp": ts(),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": is_staff,
        "confidence": 0.95,
        "metadata": {},
    }


def zone_event(visitor_id: str, zone: str = "DERMDOC", store_id: str = STORE) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ZONE_1",
        "visitor_id": visitor_id,
        "event_type": "ZONE_ENTER",
        "timestamp": ts(),
        "zone_id": zone,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.93,
        "metadata": {},
    }


def billing_event(visitor_id: str, depth: int = 1, store_id: str = STORE) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_BILLING",
        "visitor_id": visitor_id,
        "event_type": "BILLING_QUEUE_JOIN",
        "timestamp": ts(),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.91,
        "metadata": {"queue_depth": depth},
    }


def reentry_event(visitor_id: str, store_id: str = STORE) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY",
        "visitor_id": visitor_id,
        "event_type": "REENTRY",
        "timestamp": ts(),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.88,
        "metadata": {},
    }


# ── Zero-state ────────────────────────────────────────────────────────────

def test_funnel_empty_store_returns_stages():
    resp = client.get(f"/stores/EMPTY_FUNNEL_STORE/funnel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["store_id"] == "EMPTY_FUNNEL_STORE"
    assert isinstance(body["stages"], list)


# ── Happy path funnel ─────────────────────────────────────────────────────

def test_funnel_basic_pipeline():
    """3 visitors enter → 2 visit zones → 1 goes to billing."""
    store = "ST_FUNNEL_BASIC"
    vis1, vis2, vis3 = [f"VIS_{uuid.uuid4().hex[:6]}" for _ in range(3)]
    ingest([
        entry_event(vis1, store_id=store),
        entry_event(vis2, store_id=store),
        entry_event(vis3, store_id=store),
        zone_event(vis1, store_id=store),
        zone_event(vis2, store_id=store),
        billing_event(vis1, store_id=store),
    ])
    resp = client.get(f"/stores/{store}/funnel")
    assert resp.status_code == 200
    body = resp.json()
    stages = {s["stage"]: s["count"] for s in body["stages"]}
    assert stages["Entry"] == 3
    assert stages["Zone Visit"] == 2
    assert stages["Billing Queue"] == 1


# ── REENTRY not double-counted ────────────────────────────────────────────

def test_funnel_reentry_not_double_counted():
    """A REENTRY uses the same visitor_id — must not inflate Entry count."""
    store = "ST_FUNNEL_REENTRY"
    vis = f"VIS_{uuid.uuid4().hex[:6]}"
    ingest([
        entry_event(vis, store_id=store),
        reentry_event(vis, store_id=store),  # same visitor
    ])
    resp = client.get(f"/stores/{store}/funnel")
    stages = {s["stage"]: s["count"] for s in resp.json()["stages"]}
    assert stages["Entry"] == 1  # REENTRY reuses visitor_id → COUNT DISTINCT = 1


# ── Staff excluded ────────────────────────────────────────────────────────

def test_funnel_staff_excluded_from_entry_count():
    store = "ST_FUNNEL_STAFF"
    cust = f"VIS_{uuid.uuid4().hex[:6]}"
    staff = f"STAFF_{uuid.uuid4().hex[:6]}"
    ingest([
        entry_event(cust, store_id=store),
        entry_event(staff, is_staff=True, store_id=store),
    ])
    resp = client.get(f"/stores/{store}/funnel")
    stages = {s["stage"]: s["count"] for s in resp.json()["stages"]}
    assert stages["Entry"] == 1  # staff excluded


# ── Drop-off percentages ──────────────────────────────────────────────────

def test_funnel_dropoff_pct_within_0_100():
    store = "ST_FUNNEL_DROPOFF"
    v1, v2 = [f"VIS_{uuid.uuid4().hex[:6]}" for _ in range(2)]
    ingest([entry_event(v1, store_id=store), entry_event(v2, store_id=store),
            zone_event(v1, store_id=store)])
    resp = client.get(f"/stores/{store}/funnel")
    for stage in resp.json()["stages"]:
        assert 0.0 <= stage["drop_off_pct"] <= 100.0


# ── Integrity: output varies with input ───────────────────────────────────

@pytest.mark.parametrize("n_visitors,expected_entry", [(1, 1), (4, 4)])
def test_funnel_output_varies_with_input(n_visitors, expected_entry):
    store = f"ST_FUNNEL_VARY_{n_visitors}"
    events = [entry_event(f"VIS_{uuid.uuid4().hex[:6]}", store_id=store) for _ in range(n_visitors)]
    ingest(events)
    resp = client.get(f"/stores/{store}/funnel")
    stages = {s["stage"]: s["count"] for s in resp.json()["stages"]}
    assert stages["Entry"] == expected_entry
