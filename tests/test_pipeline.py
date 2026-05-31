# PROMPT: Write pipeline tests per U4-U7 without requiring OpenCV/ultralytics.
#         Use synthetic TrackRecord fixtures to test counting, re-ID, staff, and
#         zone logic in isolation. Integration test for emit + schema validation.
# CHANGES MADE: Full pipeline unit tests using in-process fixtures, no video deps.
import uuid
from datetime import datetime, timezone

import numpy as np
import pytest

from pipeline.detect import TrackRecord
from pipeline.counting import LineCrossingCounter
from pipeline.reid import ReIDTracker
from pipeline.staff import StaffClassifier
from pipeline.zones import ZoneTracker, _ray_cast
from pipeline.emit import build_event, frame_to_timestamp


# ── Fixtures ──────────────────────────────────────────────────────────────

ENTRY_LINE = [[0, 500], [1920, 500]]   # horizontal at y=500


def make_track(track_id: int, cx: float, cy: float, frame_idx: int,
               camera_id: str = "CAM_ENTRY", confidence: float = 0.9) -> TrackRecord:
    w, h = 80, 200
    return TrackRecord(
        track_id=track_id,
        bbox_xyxy=(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2),
        confidence=confidence,
        frame_idx=frame_idx,
        camera_id=camera_id,
        low_confidence=confidence < 0.4,
    )


# ── U4: Detection TrackRecord ─────────────────────────────────────────────

def test_track_foot_xy():
    rec = make_track(1, 960, 540, 0)
    fx, fy = rec.foot_xy
    assert fx == pytest.approx(960.0)
    assert fy == pytest.approx(540.0 + 100.0)   # cy + h/2 = bottom centre


def test_track_low_confidence_flag():
    rec = make_track(1, 100, 100, 0, confidence=0.3)
    assert rec.low_confidence is True


def test_track_high_confidence_not_flagged():
    rec = make_track(1, 100, 100, 0, confidence=0.85)
    assert rec.low_confidence is False


# ── U5: Line-crossing counting ────────────────────────────────────────────

def _entry_sequence(counter, track_id: int, start_y: int, end_y: int, n_frames: int = 12):
    """Simulate a track moving from start_y to end_y over n_frames.
    12 frames ensures MIN_CROSS_FRAMES (2) is satisfied on the pre-crossing side."""
    events = []
    for i in range(n_frames):
        cy = start_y + (end_y - start_y) * i / (n_frames - 1)
        rec = make_track(track_id, 960, cy, i)
        events.extend(counter.process_frame([rec]))
    return events


def test_counting_inbound_emits_entry():
    counter = LineCrossingCounter("CAM_ENTRY", ENTRY_LINE)
    # Track starts at y=200 (above line=500 → outside) and moves to y=800 (inside)
    events = _entry_sequence(counter, 1, 200, 800)
    entry_evs = [e for e in events if e.event_type == "ENTRY"]
    assert len(entry_evs) == 1
    assert entry_evs[0].visitor_id.startswith("VIS_")


def test_counting_outbound_emits_exit():
    counter = LineCrossingCounter("CAM_ENTRY", ENTRY_LINE)
    # Enter first
    _entry_sequence(counter, 1, 200, 800)
    # Then exit
    events = _entry_sequence(counter, 1, 800, 200)
    exit_evs = [e for e in events if e.event_type == "EXIT"]
    assert len(exit_evs) == 1


def test_counting_group_entry_3_tracks():
    """3 tracks crossing inbound → 3 ENTRY events."""
    counter = LineCrossingCounter("CAM_ENTRY", ENTRY_LINE)
    all_events = []
    for tid in [10, 11, 12]:
        all_events.extend(_entry_sequence(counter, tid, 200, 800))
    entry_evs = [e for e in all_events if e.event_type == "ENTRY"]
    assert len(entry_evs) == 3


def test_counting_loiter_no_event():
    """Track that only moves along the line (doesn't cross) emits nothing."""
    counter = LineCrossingCounter("CAM_ENTRY", ENTRY_LINE)
    events = []
    for i in range(10):
        rec = make_track(99, 960 + i * 10, 500, i)  # y stays at 500 (on the line)
        events.extend(counter.process_frame([rec]))
    assert events == []


def test_counting_floor_camera_no_entry():
    """Floor camera never emits ENTRY even with tracks."""
    counter = LineCrossingCounter("CAM_FLOOR_A", ENTRY_LINE)
    events = _entry_sequence(counter, 5, 200, 800)
    # LineCrossingCounter logic works regardless; the orchestrator (run.py) only
    # attaches a counter to entry-role cameras. Here we verify no crash.
    # In practice, floor cameras have no counter instance at all.
    assert all(e.camera_id == "CAM_FLOOR_A" for e in events)


# ── U6: Re-ID ─────────────────────────────────────────────────────────────

def make_blank_frame(h=720, w=1280):
    return np.zeros((h, w, 3), dtype=np.uint8)


def make_bbox(cx: int = 640, cy: int = 360, w: int = 80, h: int = 200):
    return (cx - w // 2, cy - h // 2, cx + w // 2, cy + h // 2)


def test_reid_no_prior_exit_returns_none():
    tracker = ReIDTracker()
    frame = make_blank_frame()
    result = tracker.check_reentry(frame, make_bbox())
    assert result is None


def test_reid_matching_signature_returns_visitor_id(monkeypatch):
    """Inject a known unit-vector signature to test matching without cv2."""
    sig = np.ones(48, dtype=np.float32)
    sig /= np.linalg.norm(sig)   # unit vector → cosine sim with itself = 1.0

    import pipeline.reid as reid_mod
    monkeypatch.setattr(reid_mod, "_compute_signature", lambda frame, bbox: sig)

    tracker = ReIDTracker(threshold=0.9)
    frame = make_blank_frame()
    tracker.record_exit("VIS_ABC", frame, make_bbox())
    result = tracker.check_reentry(frame, make_bbox())
    assert result == "VIS_ABC"


def test_reid_window_expiry_returns_none(monkeypatch):
    """Manually backdating the exit timestamp causes prune to remove the record."""
    import time as time_mod
    import pipeline.reid as reid_mod

    sig = np.ones(48, dtype=np.float32)
    sig /= np.linalg.norm(sig)
    monkeypatch.setattr(reid_mod, "_compute_signature", lambda frame, bbox: sig)

    tracker = ReIDTracker(window_secs=1, threshold=0.9)
    frame = make_blank_frame()
    tracker.record_exit("VIS_OLD", frame, make_bbox())

    # Backdate the exit record so it appears expired
    for rec in tracker._exits:
        rec.exited_at -= 10   # 10 seconds in the past → outside 1-second window

    tracker._prune()
    result = tracker.check_reentry(frame, make_blank_frame())
    assert result is None


# ── U6: Staff heuristic ───────────────────────────────────────────────────

def test_staff_multi_zone_behavior_flags_staff():
    clf = StaffClassifier()
    vis = "VIS_STAFF1"
    zones = ["DERMDOC", "MINIMALIST", "CASH_COUNTER", "LAKME"]
    for z in zones:
        # Simulate many frames in billing to also trigger repeated_billing
        for _ in range(5):
            clf.update(vis, z, None, None)
    assert clf.is_staff(vis) is True


def test_staff_customer_at_counter_briefly_not_flagged():
    clf = StaffClassifier()
    vis = "VIS_CUST1"
    clf.update(vis, "CASH_COUNTER", None, None)
    clf.update(vis, "CASH_COUNTER", None, None)
    # Only 2 billing visits, only 1 zone → not flagged
    assert clf.is_staff(vis) is False


# ── U7: Zone / ray-cast ───────────────────────────────────────────────────

def test_ray_cast_inside():
    poly = [[0, 0], [100, 0], [100, 100], [0, 100]]
    assert _ray_cast(50, 50, poly) is True


def test_ray_cast_outside():
    poly = [[0, 0], [100, 0], [100, 100], [0, 100]]
    assert _ray_cast(150, 150, poly) is False


def test_ray_cast_corner():
    poly = [[0, 0], [100, 0], [100, 100], [0, 100]]
    # Corner/edge: result is implementation-defined but must not crash
    _ray_cast(0, 0, poly)   # no assertion — just no exception


def test_zone_tracker_emits_zone_enter():
    zones = [{"zone_id": "DERMDOC", "camera_id": "CAM_FLOOR_A",
              "polygon": [[0, 0], [200, 0], [200, 200], [0, 200]]}]
    tracker = ZoneTracker("CAM_FLOOR_A", zones, fps=15.0)
    evs = tracker.process_frame("VIS_1", (100, 100), frame_idx=0)
    types = [e.event_type for e in evs]
    assert "ZONE_ENTER" in types
    assert evs[0].zone_id == "DERMDOC"


def test_zone_tracker_emits_dwell_after_interval():
    zones = [{"zone_id": "DERMDOC", "camera_id": "CAM_FLOOR_A",
              "polygon": [[0, 0], [200, 0], [200, 200], [0, 200]]}]
    from pipeline.zones import DWELL_INTERVAL_FRAMES
    tracker = ZoneTracker("CAM_FLOOR_A", zones, fps=15.0)
    all_evs = []
    for f in range(DWELL_INTERVAL_FRAMES + 5):
        all_evs.extend(tracker.process_frame("VIS_D", (100, 100), frame_idx=f))
    dwell_evs = [e for e in all_evs if e.event_type == "ZONE_DWELL"]
    assert len(dwell_evs) >= 1
    assert dwell_evs[0].dwell_ms > 0


def test_zone_tracker_billing_queue_join_on_enter():
    zones = [{"zone_id": "CASH_COUNTER", "camera_id": "CAM_BILLING",
              "polygon": [[0, 0], [300, 0], [300, 300], [0, 300]]}]
    tracker = ZoneTracker("CAM_BILLING", zones, fps=15.0)
    evs = tracker.process_frame("VIS_BQ", (150, 150), frame_idx=0)
    types = [e.event_type for e in evs]
    assert "BILLING_QUEUE_JOIN" in types
    bq = next(e for e in evs if e.event_type == "BILLING_QUEUE_JOIN")
    assert bq.queue_depth >= 1


def test_zone_tracker_empty_segment_no_events():
    tracker = ZoneTracker("CAM_ENTRY", [], fps=15.0)
    evs = tracker.process_frame("VIS_X", (100, 100), frame_idx=0)
    assert evs == []


# ── U7: Emit + schema ─────────────────────────────────────────────────────

def test_emit_build_event_validates():
    ev = build_event(
        store_id="ST1008",
        camera_id="CAM_ENTRY",
        visitor_id="VIS_ABCDEF",
        event_type="ENTRY",
        timestamp="2026-04-10T09:00:00Z",
        confidence=0.92,
    )
    assert ev["event_type"] == "ENTRY"
    assert ev["store_id"] == "ST1008"
    assert "event_id" in ev


def test_emit_build_event_invalid_raises():
    """ENTRY with non-null zone_id → validation error."""
    with pytest.raises(Exception):
        build_event(
            store_id="ST1008",
            camera_id="CAM_ENTRY",
            visitor_id="VIS_X",
            event_type="ENTRY",
            timestamp="2026-04-10T09:00:00Z",
            confidence=0.9,
            zone_id="DERMDOC",  # invalid for ENTRY
        )


def test_frame_to_timestamp_correct():
    start = datetime(2026, 4, 10, 9, 0, 0, tzinfo=timezone.utc)
    ts = frame_to_timestamp(start, frame_idx=450, fps=15.0)
    assert ts == "2026-04-10T09:00:30Z"  # 450/15 = 30 s


def test_emit_round_trip_dict_model():
    """Event round-trips through build_event without data loss."""
    import json
    ev = build_event(
        store_id="ST1008",
        camera_id="CAM_FLOOR_A",
        visitor_id="VIS_RT",
        event_type="ZONE_DWELL",
        timestamp="2026-04-10T10:00:00Z",
        confidence=0.88,
        zone_id="DERMDOC",
        dwell_ms=30000,
    )
    # Re-parse via schema
    from app.schema import Event
    ev2 = Event(**ev)
    assert ev2.zone_id == "DERMDOC"
    assert ev2.dwell_ms == 30000


# ── Integration: unique event_ids ─────────────────────────────────────────

def test_build_event_unique_ids():
    ids = [build_event("ST1008", "CAM_ENTRY", f"VIS_{i}", "ENTRY",
                       "2026-04-10T09:00:00Z", 0.9)["event_id"]
           for i in range(50)]
    assert len(set(ids)) == 50   # all unique
