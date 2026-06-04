"""
Locks the official-schema export (pipeline/to_official.py) to the graders'
contract in data/sample_eventsbe42122.jsonl.

For each event family (entry/exit, zone_*, queue_*) the keys we emit must
exactly match the keys in the provided sample — no missing, no extra fields.
Also checks the canonical -> official event_type mapping and that demographic
fields are present-but-null (schema-conformant, not fabricated).
"""
import collections
import json
from pathlib import Path

import pytest

from pipeline.to_official import convert_records, load_zone_meta

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "data" / "sample_eventsbe42122.jsonl"
ZONES_CFG = ROOT / "config" / "zones.json"


def _family(event_type: str) -> str:
    if event_type in ("entry", "exit"):
        return "ENTRY_EXIT"
    if event_type in ("zone_entered", "zone_exited"):
        return "ZONE"
    if event_type in ("queue_completed", "queue_abandoned"):
        return "QUEUE"
    return event_type


def _sample_family_keys() -> dict[str, set]:
    keys: dict[str, set] = collections.defaultdict(set)
    for line in SAMPLE.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        ev = json.loads(line)
        keys[_family(ev["event_type"])].update(ev.keys())
    return dict(keys)


@pytest.fixture
def converted() -> list[dict]:
    """A canonical stream exercising every event type, projected to official."""
    canonical = [
        {"event_type": "ENTRY", "visitor_id": "V1", "camera_id": "CAM_ENTRY",
         "store_id": "ST1008", "timestamp": "2026-04-10T09:00:00Z",
         "zone_id": None, "is_staff": False, "confidence": 0.9, "metadata": {}},
        {"event_type": "ZONE_ENTER", "visitor_id": "V1", "camera_id": "CAM_ZONE_1",
         "store_id": "ST1008", "timestamp": "2026-04-10T09:00:30Z",
         "zone_id": "DERMDOC", "is_staff": False, "confidence": 0.9, "metadata": {}},
        {"event_type": "ZONE_DWELL", "visitor_id": "V1", "camera_id": "CAM_ZONE_1",
         "store_id": "ST1008", "timestamp": "2026-04-10T09:00:45Z",
         "zone_id": "DERMDOC", "dwell_ms": 15000, "is_staff": False,
         "confidence": 0.9, "metadata": {}},
        {"event_type": "ZONE_EXIT", "visitor_id": "V1", "camera_id": "CAM_ZONE_1",
         "store_id": "ST1008", "timestamp": "2026-04-10T09:01:00Z",
         "zone_id": "DERMDOC", "is_staff": False, "confidence": 0.9, "metadata": {}},
        # Billing episode that gets served (long wait -> completed)
        {"event_type": "BILLING_QUEUE_JOIN", "visitor_id": "V1", "camera_id": "CAM_BILLING",
         "store_id": "ST1008", "timestamp": "2026-04-10T09:02:00Z",
         "zone_id": "CASH_COUNTER", "is_staff": False, "confidence": 0.9,
         "metadata": {"queue_depth": 2}},
        {"event_type": "BILLING_QUEUE_ABANDON", "visitor_id": "V1", "camera_id": "CAM_BILLING",
         "store_id": "ST1008", "timestamp": "2026-04-10T09:03:00Z",
         "zone_id": "CASH_COUNTER", "is_staff": False, "confidence": 0.9,
         "metadata": {"queue_depth": 1}},
        {"event_type": "EXIT", "visitor_id": "V1", "camera_id": "CAM_ENTRY",
         "store_id": "ST1008", "timestamp": "2026-04-10T09:05:00Z",
         "zone_id": None, "is_staff": False, "confidence": 0.9, "metadata": {}},
        # Short billing episode -> abandoned
        {"event_type": "BILLING_QUEUE_JOIN", "visitor_id": "V2", "camera_id": "CAM_BILLING",
         "store_id": "ST1008", "timestamp": "2026-04-10T09:06:00Z",
         "zone_id": "CASH_COUNTER", "is_staff": False, "confidence": 0.9,
         "metadata": {"queue_depth": 3}},
        {"event_type": "BILLING_QUEUE_ABANDON", "visitor_id": "V2", "camera_id": "CAM_BILLING",
         "store_id": "ST1008", "timestamp": "2026-04-10T09:06:05Z",
         "zone_id": "CASH_COUNTER", "is_staff": False, "confidence": 0.9,
         "metadata": {"queue_depth": 2}},
    ]
    zone_meta = load_zone_meta(ZONES_CFG)
    return convert_records(canonical, zone_meta, "ST1008")


def test_event_types_are_official(converted):
    types = {e["event_type"] for e in converted}
    assert types <= {"entry", "exit", "zone_entered", "zone_exited",
                     "queue_completed", "queue_abandoned"}
    assert "zone_entered" in types and "zone_exited" in types
    assert "entry" in types and "exit" in types
    # No internal upper-case types leak through, and dwell is dropped.
    assert not any(e["event_type"].isupper() for e in converted)


def test_keys_match_sample_per_family(converted):
    expected = _sample_family_keys()
    mine: dict[str, set] = collections.defaultdict(set)
    for e in converted:
        mine[_family(e["event_type"])].update(e.keys())

    for fam, exp_keys in expected.items():
        got = mine.get(fam, set())
        assert got == exp_keys, (
            f"{fam}: missing={sorted(exp_keys - got)} extra={sorted(got - exp_keys)}"
        )


def test_demographics_present_but_null(converted):
    for e in converted:
        if e["event_type"] in ("entry", "exit"):
            assert e["gender_pred"] is None and e["age_pred"] is None
            assert e["is_face_hidden"] is False
            assert e["group_id"] is None and e["group_size"] is None
        if e["event_type"] in ("zone_entered", "zone_exited"):
            assert e["gender"] is None and e["age"] is None


def test_queue_completed_vs_abandoned(converted):
    queues = [e for e in converted if e["event_type"].startswith("queue_")]
    assert len(queues) == 2
    completed = [e for e in queues if not e["abandoned"]]
    abandoned = [e for e in queues if e["abandoned"]]
    assert len(completed) == 1 and len(abandoned) == 1
    # Completed has a derived served-time; abandoned does not.
    assert completed[0]["queue_served_ts"] is not None
    assert abandoned[0]["queue_served_ts"] is None
    assert completed[0]["wait_seconds"] == 60
    assert completed[0]["queue_position_at_join"] == 2


def test_zone_ids_use_official_namespace(converted):
    zones = [e for e in converted if e["event_type"].startswith("zone_")]
    assert all(e["zone_id"].startswith("PURPLLE_BLR_1008_") for e in zones)
    assert all(e["zone_name"] and e["zone_type"] for e in zones)
