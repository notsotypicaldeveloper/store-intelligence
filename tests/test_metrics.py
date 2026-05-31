# PROMPT: Write acceptance-gate tests for U1 — zero-state metrics and health
#         endpoints must return 200 with well-typed, non-null fields even with
#         an empty SQLite database and no events.jsonl present.
# CHANGES MADE: Initial tests for R1/R2/R11 zero-state; updated store IDs to
#         isolate from shared session DB and extended to cover all stub endpoints.
import os
import tempfile
import pytest

os.environ.setdefault("DB_PATH", ":memory:")

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


# ── /health ───────────────────────────────────────────────────────────────

def test_health_returns_200_on_empty_db():
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert isinstance(body["stores"], dict)


# ── /stores/{id}/metrics zero-state ──────────────────────────────────────

def test_metrics_empty_db_returns_200():
    resp = client.get("/stores/ST1008/metrics")
    assert resp.status_code == 200


def test_metrics_zero_state_fields_are_typed_not_null():
    # Use a store ID no other test touches to avoid shared-DB state bleed
    resp = client.get("/stores/ST_ZERO_STATE_ONLY/metrics")
    body = resp.json()
    assert body["store_id"] == "ST_ZERO_STATE_ONLY"
    assert isinstance(body["unique_visitors"], int)
    assert body["unique_visitors"] == 0
    assert isinstance(body["conversion_rate"], float)
    assert body["conversion_rate"] == 0.0
    assert isinstance(body["avg_dwell_per_zone"], dict)
    assert isinstance(body["queue_depth"], int)
    assert isinstance(body["abandonment_rate"], float)


def test_metrics_no_division_by_zero_on_empty_store():
    """conversion_rate and abandonment_rate must not crash on zero traffic."""
    resp = client.get("/stores/ST_TRULY_UNKNOWN/metrics")
    assert resp.status_code == 200
    body = resp.json()
    assert body["store_id"] == "ST_TRULY_UNKNOWN"
    assert body["conversion_rate"] == 0.0
    assert body["abandonment_rate"] == 0.0


# ── /stores/{id}/funnel zero-state ───────────────────────────────────────

def test_funnel_empty_db_returns_200():
    resp = client.get("/stores/ST1008/funnel")
    assert resp.status_code == 200
    body = resp.json()
    assert body["store_id"] == "ST1008"
    assert isinstance(body["stages"], list)


# ── /stores/{id}/heatmap zero-state ──────────────────────────────────────

def test_heatmap_empty_db_returns_200():
    resp = client.get("/stores/ST1008/heatmap")
    assert resp.status_code == 200
    body = resp.json()
    assert body["store_id"] == "ST1008"
    assert isinstance(body["zones"], list)


# ── /stores/{id}/anomalies zero-state ────────────────────────────────────

def test_anomalies_empty_db_returns_200():
    resp = client.get("/stores/ST1008/anomalies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["store_id"] == "ST1008"
    assert isinstance(body["anomalies"], list)
