# PROMPT: Write tests for POS conversion correlation per R16.
#         Two methods: time-window (billing zone 5 min before transaction)
#         and brand-dwell (session in brand zone + matching brand purchase).
#         Edge cases: no sessions, zero purchases, phantom conversion prevention.
# CHANGES MADE: Uses monkeypatch to replace _load_pos_df; no lru_cache coupling.
import csv
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def ts(dt: datetime) -> str:
    return dt.isoformat()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ingest_events(events: list[dict]):
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200


def make_entry(visitor_id: str, store_id: str, t: datetime) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY",
        "visitor_id": visitor_id,
        "event_type": "ENTRY",
        "timestamp": ts(t),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.95,
        "metadata": {},
    }


def make_billing(visitor_id: str, store_id: str, t: datetime) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_BILLING",
        "visitor_id": visitor_id,
        "event_type": "BILLING_QUEUE_JOIN",
        "timestamp": ts(t),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.90,
        "metadata": {"queue_depth": 1},
    }


def make_zone_enter(visitor_id: str, store_id: str, zone: str, t: datetime) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ZONE_1",
        "visitor_id": visitor_id,
        "event_type": "ZONE_ENTER",
        "timestamp": ts(t),
        "zone_id": zone,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.92,
        "metadata": {},
    }


def fake_pos_df(brand_name: str, order_date: str, order_time: str, store_id: str = "ST1008") -> pd.DataFrame:
    """Return a minimal parsed POS DataFrame like _load_pos_df would produce."""
    from app.conversion import _BRAND_TO_ZONE
    row = {
        "order_id": "TEST001",
        "order_date": order_date,
        "order_time": order_time,
        "store_id": store_id,
        "brand_name": brand_name,
        "sub_category": "",
    }
    df = pd.DataFrame([row])
    df["order_dt"] = pd.to_datetime(
        df["order_date"].str.strip() + " " + df["order_time"].str.strip(),
        format="%d-%m-%Y %H:%M:%S",
        errors="coerce",
    ).dt.tz_localize("Asia/Kolkata").dt.tz_convert("UTC")
    df = df.dropna(subset=["order_dt"])
    df["brand_zone"] = df["brand_name"].str.strip().map(_BRAND_TO_ZONE)
    return df


# ── Time-window conversion ────────────────────────────────────────────────

def test_conversion_time_window_basic(monkeypatch):
    """Session at billing 3 min before a transaction → converted."""
    from app import conversion
    pos = fake_pos_df("Good Vibes", "10-04-2026", "12:00:00", store_id="ST_CONV_TW")
    monkeypatch.setattr(conversion, "_load_pos_df", lambda data_dir="data": pos)

    store = "ST_CONV_TW"
    t_purchase = datetime(2026, 4, 10, 6, 30, 0, tzinfo=timezone.utc)  # 12:00 IST = 06:30 UTC
    t_billing = t_purchase - timedelta(minutes=3)
    v = f"VIS_{uuid.uuid4().hex[:6]}"
    ingest_events([make_entry(v, store, t_billing - timedelta(minutes=5)),
                   make_billing(v, store, t_billing)])
    resp = client.get(f"/stores/{store}/metrics")
    assert resp.status_code == 200
    assert resp.json()["conversion_rate"] > 0.0


def test_conversion_no_phantom_attribution(monkeypatch):
    """Transaction with no session within 5 min → no attribution from time-window."""
    from app import conversion
    # Transaction at 12:00 IST (06:30 UTC); customer entered at 02:00 UTC — far outside window
    pos = fake_pos_df("DERMDOC", "10-04-2026", "12:00:00", store_id="ST_CONV_NONE")
    monkeypatch.setattr(conversion, "_load_pos_df", lambda data_dir="data": pos)

    store = "ST_CONV_NONE"
    # Enter store at 02:00 UTC — 4.5 hours before the transaction, no billing event
    t = datetime(2026, 4, 10, 2, 0, 0, tzinfo=timezone.utc)
    v = f"VIS_{uuid.uuid4().hex[:6]}"
    ingest_events([make_entry(v, store, t)])
    resp = client.get(f"/stores/{store}/metrics")
    assert resp.status_code == 200
    # No billing event in window, no brand-zone dwell for DERMDOC → 0.0
    assert resp.json()["conversion_rate"] == 0.0


def test_conversion_zero_purchases_no_crash(monkeypatch):
    """Empty POS data → conversion rate = 0."""
    from app import conversion
    monkeypatch.setattr(conversion, "_load_pos_df", lambda data_dir="data": pd.DataFrame())

    store = "ST_CONV_ZERO"
    v = f"VIS_{uuid.uuid4().hex[:6]}"
    ingest_events([make_entry(v, store, datetime.now(timezone.utc))])
    resp = client.get(f"/stores/{store}/metrics")
    assert resp.status_code == 200
    assert resp.json()["conversion_rate"] == 0.0


# ── Brand-dwell conversion ────────────────────────────────────────────────

def test_conversion_brand_dwell_match(monkeypatch):
    """Session in DERMDOC zone + DERMDOC transaction → brand-attributed."""
    from app import conversion
    pos = fake_pos_df("DERMDOC", "10-04-2026", "12:00:00", store_id="ST_CONV_BD")
    monkeypatch.setattr(conversion, "_load_pos_df", lambda data_dir="data": pos)

    store = "ST_CONV_BD"
    t = datetime(2026, 4, 10, 3, 0, 0, tzinfo=timezone.utc)
    v = f"VIS_{uuid.uuid4().hex[:6]}"
    ingest_events([make_entry(v, store, t), make_zone_enter(v, store, "DERMDOC", t)])
    resp = client.get(f"/stores/{store}/metrics")
    assert resp.status_code == 200
    assert resp.json()["conversion_rate"] > 0.0


# ── Integrity: output changes with different inputs ───────────────────────

@pytest.mark.parametrize("has_pos", [False, True])
def test_conversion_rate_varies_with_input(has_pos, monkeypatch):
    from app import conversion
    suffix = "Y" if has_pos else "N"
    store = f"ST_CONV_VAR_{suffix}"
    if has_pos:
        pos = fake_pos_df("DERMDOC", "10-04-2026", "12:00:00", store_id=store)
    else:
        pos = pd.DataFrame()
    monkeypatch.setattr(conversion, "_load_pos_df", lambda data_dir="data": pos)

    t = datetime(2026, 4, 10, 3, 0, 0, tzinfo=timezone.utc)
    v = f"VIS_{uuid.uuid4().hex[:6]}"
    ingest_events([make_entry(v, store, t), make_zone_enter(v, store, "DERMDOC", t)])
    resp = client.get(f"/stores/{store}/metrics")
    rate = resp.json()["conversion_rate"]
    assert 0.0 <= rate <= 1.0
    if not has_pos:
        assert rate == 0.0
    if has_pos:
        assert rate > 0.0  # brand-dwell match should fire
