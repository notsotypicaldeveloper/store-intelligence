# PROMPT: Write tests for anomaly detection endpoint per R14.
#         Must detect: queue spike (CRITICAL/WARN), dead zone (no visits 30 min),
#         conversion drop. Edge cases: empty store no crash, quiet zone vs dead zone.
# CHANGES MADE: Anomaly tests covering queue spike, dead zones, zero-state, health R15.
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def ts_offset(minutes: int = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def ingest(events):
    r = client.post("/events/ingest", json={"events": events})
    assert r.status_code == 200


def billing_join(visitor_id: str, store_id: str, depth: int, minutes_ago: int = 0) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_BILLING",
        "visitor_id": visitor_id,
        "event_type": "BILLING_QUEUE_JOIN",
        "timestamp": ts_offset(minutes_ago),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.9,
        "metadata": {"queue_depth": depth},
    }


def zone_enter(visitor_id: str, store_id: str, zone: str, minutes_ago: int = 0) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ZONE_1",
        "visitor_id": visitor_id,
        "event_type": "ZONE_ENTER",
        "timestamp": ts_offset(minutes_ago),
        "zone_id": zone,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.91,
        "metadata": {},
    }


# ── Zero-state ────────────────────────────────────────────────────────────

def test_anomalies_empty_store_no_crash():
    resp = client.get("/stores/ST_ANOM_EMPTY/anomalies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["store_id"] == "ST_ANOM_EMPTY"
    assert isinstance(body["anomalies"], list)


# ── Queue spike ───────────────────────────────────────────────────────────

def test_anomalies_queue_spike_critical():
    store = "ST_ANOM_QCRIT"
    v = f"VIS_{uuid.uuid4().hex[:6]}"
    ingest([billing_join(v, store, depth=6)])
    resp = client.get(f"/stores/{store}/anomalies")
    anomalies = resp.json()["anomalies"]
    queue_anoms = [a for a in anomalies if a["anomaly_type"] == "QUEUE_SPIKE"]
    assert len(queue_anoms) >= 1
    assert queue_anoms[0]["severity"] == "CRITICAL"
    assert "suggested_action" in queue_anoms[0]
    assert queue_anoms[0]["suggested_action"]


def test_anomalies_queue_spike_warn():
    store = "ST_ANOM_QWARN"
    v = f"VIS_{uuid.uuid4().hex[:6]}"
    ingest([billing_join(v, store, depth=3)])
    resp = client.get(f"/stores/{store}/anomalies")
    anomalies = resp.json()["anomalies"]
    queue_anoms = [a for a in anomalies if a["anomaly_type"] == "QUEUE_SPIKE"]
    assert len(queue_anoms) >= 1
    assert queue_anoms[0]["severity"] == "WARN"


def test_anomalies_no_spike_at_low_queue():
    store = "ST_ANOM_QLOW"
    v = f"VIS_{uuid.uuid4().hex[:6]}"
    ingest([billing_join(v, store, depth=1)])
    resp = client.get(f"/stores/{store}/anomalies")
    queue_anoms = [a for a in resp.json()["anomalies"] if a["anomaly_type"] == "QUEUE_SPIKE"]
    assert len(queue_anoms) == 0


# ── Dead zone ─────────────────────────────────────────────────────────────

def test_anomalies_dead_zone_detected():
    """Zone visited >30 min ago → dead-zone anomaly."""
    store = "ST_ANOM_DEAD"
    v = f"VIS_{uuid.uuid4().hex[:6]}"
    ingest([zone_enter(v, store, "DERMDOC", minutes_ago=35)])
    resp = client.get(f"/stores/{store}/anomalies")
    dead = [a for a in resp.json()["anomalies"] if a["anomaly_type"] == "DEAD_ZONE"]
    assert len(dead) >= 1
    assert dead[0]["severity"] == "INFO"
    assert "DERMDOC" in dead[0]["description"]


def test_anomalies_no_dead_zone_if_recent_visit():
    """Zone visited <30 min ago → no dead-zone anomaly."""
    store = "ST_ANOM_LIVE"
    v = f"VIS_{uuid.uuid4().hex[:6]}"
    ingest([zone_enter(v, store, "MINIMALIST", minutes_ago=5)])
    resp = client.get(f"/stores/{store}/anomalies")
    dead = [a for a in resp.json()["anomalies"] if a["anomaly_type"] == "DEAD_ZONE"]
    assert len(dead) == 0


# ── Anomaly response schema ───────────────────────────────────────────────

def test_anomaly_schema_fields():
    store = "ST_ANOM_SCHEMA"
    v = f"VIS_{uuid.uuid4().hex[:6]}"
    ingest([billing_join(v, store, depth=5)])
    resp = client.get(f"/stores/{store}/anomalies")
    anoms = resp.json()["anomalies"]
    assert len(anoms) > 0
    for a in anoms:
        assert "anomaly_type" in a
        assert a["severity"] in ("INFO", "WARN", "CRITICAL")
        assert "description" in a
        assert "suggested_action" in a
        assert "detected_at" in a


# ── Health endpoint ───────────────────────────────────────────────────────

def test_health_returns_ok_with_events():
    store = "ST_HEALTH_TEST"
    v = f"VIS_{uuid.uuid4().hex[:6]}"
    ingest([{
        "event_id": str(uuid.uuid4()),
        "store_id": store,
        "camera_id": "CAM_ENTRY",
        "visitor_id": v,
        "event_type": "ENTRY",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.9,
        "metadata": {},
    }])
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert store in body["stores"]
    assert not body["stores"][store]["stale_feed"]


def test_health_stale_feed_detected():
    """Injecting an old event timestamp → STALE_FEED = true."""
    store = "ST_HEALTH_STALE"
    v = f"VIS_{uuid.uuid4().hex[:6]}"
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
    ingest([{
        "event_id": str(uuid.uuid4()),
        "store_id": store,
        "camera_id": "CAM_ENTRY",
        "visitor_id": v,
        "event_type": "ENTRY",
        "timestamp": old_ts,
        "zone_id": None,
        "dwell_ms": 0,
        "is_staff": False,
        "confidence": 0.9,
        "metadata": {},
    }])
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["stores"][store]["stale_feed"] is True
