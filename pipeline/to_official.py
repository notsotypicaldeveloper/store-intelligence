#!/usr/bin/env python3
"""
Map the pipeline's internal (canonical) event stream to the Purplle
sample_events.jsonl schema.

The internal schema (app/schema.py) is what the analytics API + tests run on.
The graders' contract is data/sample_eventsbe42122.jsonl — a *different, richer*
shape. Rather than destabilise the tested API by switching its schema, we keep
the canonical stream as the source of truth and project it into the official
shape here. That makes the official output a pure, reproducible function of the
canonical events (run it standalone, no Docker / OpenCV needed):

    python pipeline/to_official.py \
        --input events/events.jsonl \
        --output events/events.official.jsonl

Mapping summary (canonical -> official):
    ENTRY                 -> entry
    EXIT                  -> exit
    REENTRY               -> entry          (same id_token: a returning visitor)
    ZONE_ENTER            -> zone_entered
    ZONE_EXIT             -> zone_exited
    ZONE_DWELL            -> (dropped: dwell is implied by the enter/exit pair)
    BILLING_QUEUE_JOIN +  -> queue_completed / queue_abandoned
    BILLING_QUEUE_ABANDON    (paired into one queue episode)

Fields the schema requires but in-store vision cannot truthfully produce are
emitted as null/false rather than fabricated:
    gender_pred / age_pred / age_bucket / gender / age   -> null
    group_id / group_size                                -> null
    is_face_hidden                                       -> false
See docs/CHOICES.md for the rationale.
"""
from __future__ import annotations

import argparse
import json
import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# A billing episode shorter than this is treated as "left before being served"
# (abandoned); longer episodes are treated as completed. This is a heuristic —
# in production the API reconciles billing episodes against the POS feed
# (app/conversion.py) to confirm true abandonment. See docs/CHOICES.md.
MIN_SERVICE_SECONDS = 20


# ── timestamp helpers ────────────────────────────────────────────────────────

def _to_official_ts(ts: str) -> str:
    """Canonical '...Z' / ISO -> sample style '2026-03-08T18:10:05.120000'."""
    from datetime import datetime

    s = ts.strip().replace("Z", "")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Already in the target shape or unparseable — return as-is.
        return s
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")


def _epoch(ts: str) -> float:
    from datetime import datetime

    s = ts.strip().replace("Z", "")
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return 0.0


# ── zone metadata ────────────────────────────────────────────────────────────

def _centroid(polygon: list[list[float]]) -> tuple[float, float]:
    if not polygon:
        return (0.0, 0.0)
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return (round(sum(xs) / len(xs), 1), round(sum(ys) / len(ys), 1))


def load_zone_meta(zones_cfg_path: str | Path) -> dict[str, dict]:
    """Build a lookup: internal zone_id -> official zone metadata + hotspot.

    Keyed by zone_id alone (zone_ids are unique across the store), so the
    mapping is robust even if an event carries a stale/mismatched camera_id.
    """
    cfg = json.loads(Path(zones_cfg_path).read_text())
    meta: dict[str, dict] = {}
    for z in cfg.get("zones", []):
        hx, hy = _centroid(z.get("polygon", []))
        meta[z["zone_id"]] = {
            "official_id": z.get("official_id", z["zone_id"]),
            "zone_name": z.get("zone_name", z["zone_id"]),
            "zone_type": z.get("zone_type", "UNKNOWN"),
            "is_revenue_zone": z.get("is_revenue_zone", "No"),
            "hotspot_x": hx,
            "hotspot_y": hy,
        }
    return meta


def _zone_meta_for(zone_id: str | None, zone_meta: dict[str, dict]) -> dict:
    if zone_id and zone_id in zone_meta:
        return zone_meta[zone_id]
    # Graceful fallback for an unknown/stale zone id.
    return {
        "official_id": zone_id or "UNKNOWN",
        "zone_name": zone_id or "Unknown",
        "zone_type": "UNKNOWN",
        "is_revenue_zone": "No",
        "hotspot_x": 0.0,
        "hotspot_y": 0.0,
    }


# ── visitor identity registry ────────────────────────────────────────────────

class _IdRegistry:
    """Assigns stable official ids to each internal visitor_id, in first-seen
    order: id_token 'ID_60001..' for entry/exit, integer track_id 101.. for
    zone/queue events (mirrors the sample file's conventions)."""

    def __init__(self) -> None:
        self._seq: dict[str, int] = {}

    def _n(self, visitor_id: str) -> int:
        if visitor_id not in self._seq:
            self._seq[visitor_id] = len(self._seq)
        return self._seq[visitor_id]

    def id_token(self, visitor_id: str) -> str:
        return f"ID_{60001 + self._n(visitor_id)}"

    def track_id(self, visitor_id: str) -> int:
        return 101 + self._n(visitor_id)


# ── event mappers ────────────────────────────────────────────────────────────

_PERSON_NULLS = {"gender_pred": None, "age_pred": None, "age_bucket": None}


def _entry_exit(ev: dict, kind: str, store_id: str, reg: _IdRegistry) -> dict:
    return {
        "event_type": kind,  # "entry" | "exit"
        "id_token": reg.id_token(ev["visitor_id"]),
        "store_code": store_id,
        "camera_id": ev["camera_id"],
        "event_timestamp": _to_official_ts(ev["timestamp"]),
        "is_staff": bool(ev.get("is_staff", False)),
        "gender_pred": None,
        "age_pred": None,
        "age_bucket": None,
        "is_face_hidden": False,
        "group_id": None,
        "group_size": None,
    }


def _zone(ev: dict, kind: str, store_id: str, reg: _IdRegistry,
          zone_meta: dict[str, dict]) -> dict:
    zm = _zone_meta_for(ev.get("zone_id"), zone_meta)
    return {
        "event_type": kind,  # "zone_entered" | "zone_exited"
        "track_id": reg.track_id(ev["visitor_id"]),
        "store_id": store_id,
        "camera_id": ev["camera_id"],
        "zone_id": zm["official_id"],
        "zone_name": zm["zone_name"],
        "zone_type": zm["zone_type"],
        "is_revenue_zone": zm["is_revenue_zone"],
        "event_time": _to_official_ts(ev["timestamp"]),
        "zone_hotspot_x": zm["hotspot_x"],
        "zone_hotspot_y": zm["hotspot_y"],
        "gender": None,
        "age": None,
        "age_bucket": None,
    }


def _queue_episode(join: dict, close: dict, store_id: str, reg: _IdRegistry,
                   zone_meta: dict[str, dict]) -> dict:
    """Fold a BILLING_QUEUE_JOIN + its closing event into one queue_* record."""
    zm = _zone_meta_for(join.get("zone_id"), zone_meta)
    join_ts = join["timestamp"]
    exit_ts = close["timestamp"] if close else join_ts
    wait = max(0, round(_epoch(exit_ts) - _epoch(join_ts)))
    abandoned = wait < MIN_SERVICE_SECONDS
    depth = (join.get("metadata") or {}).get("queue_depth")

    return {
        "queue_event_id": str(uuid.uuid4()),
        "event_type": "queue_abandoned" if abandoned else "queue_completed",
        "track_id": reg.track_id(join["visitor_id"]),
        "store_id": store_id,
        "camera_id": join["camera_id"],
        "zone_id": zm["official_id"],
        "zone_name": zm["zone_name"],
        "zone_type": zm["zone_type"],
        "is_revenue_zone": zm["is_revenue_zone"],
        "queue_join_ts": _to_official_ts(join_ts),
        # served-time isn't directly observed; for a completed episode we take
        # it as the exit instant (served, then leaves), null when abandoned.
        "queue_served_ts": None if abandoned else _to_official_ts(exit_ts),
        "queue_exit_ts": _to_official_ts(exit_ts),
        "wait_seconds": wait,
        "queue_position_at_join": depth,
        "abandoned": abandoned,
        "zone_hotspot_x": zm["hotspot_x"],
        "zone_hotspot_y": zm["hotspot_y"],
        "gender": None,
        "age": None,
        "age_bucket": None,
    }


# ── top-level conversion ─────────────────────────────────────────────────────

def convert_records(records: list[dict], zone_meta: dict[str, dict],
                    store_id: str) -> list[dict]:
    """Project canonical event dicts into official-schema dicts, time-sorted."""
    reg = _IdRegistry()
    # Process in time order so id assignment + queue pairing follow real order.
    records = sorted(records, key=lambda e: e.get("timestamp", ""))

    out: list[dict] = []
    # Track an open billing JOIN per visitor to pair with its closing event.
    open_join: dict[str, dict] = {}

    for ev in records:
        et = ev.get("event_type")

        if et == "ENTRY":
            out.append(_entry_exit(ev, "entry", store_id, reg))
        elif et == "REENTRY":
            # A returning visitor: same id_token, surfaced as another entry.
            out.append(_entry_exit(ev, "entry", store_id, reg))
        elif et == "EXIT":
            out.append(_entry_exit(ev, "exit", store_id, reg))
        elif et == "ZONE_ENTER":
            out.append(_zone(ev, "zone_entered", store_id, reg, zone_meta))
        elif et == "ZONE_EXIT":
            out.append(_zone(ev, "zone_exited", store_id, reg, zone_meta))
        elif et == "ZONE_DWELL":
            continue  # dwell is implied by the enter/exit pair
        elif et == "BILLING_QUEUE_JOIN":
            vid = ev["visitor_id"]
            # A dangling prior join (no close seen) — flush it solo.
            if vid in open_join:
                out.append(_queue_episode(open_join[vid], None, store_id, reg, zone_meta))
            open_join[vid] = ev
        elif et == "BILLING_QUEUE_ABANDON":
            vid = ev["visitor_id"]
            join = open_join.pop(vid, None)
            if join is None:
                join = ev  # close without a join — treat as a zero-length episode
            out.append(_queue_episode(join, ev, store_id, reg, zone_meta))
        # Unknown event types are dropped (nothing in the official contract).

    # Flush any billing joins that never saw a close.
    for vid, join in open_join.items():
        out.append(_queue_episode(join, None, store_id, reg, zone_meta))

    out.sort(key=lambda e: e.get("event_timestamp")
             or e.get("event_time")
             or e.get("queue_join_ts", ""))
    return out


def convert_file(input_path: str | Path, output_path: str | Path,
                 zones_cfg_path: str | Path, store_id: str) -> int:
    records = []
    with open(input_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    zone_meta = load_zone_meta(zones_cfg_path)
    official = convert_records(records, zone_meta, store_id)

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        for rec in official:
            fh.write(json.dumps(rec) + "\n")

    logger.info("Converted %d canonical -> %d official events -> %s",
                len(records), len(official), out)
    return len(official)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(description="Map canonical events -> official sample_events schema")
    p.add_argument("--input", default="events/events.jsonl")
    p.add_argument("--output", default="events/events.official.jsonl")
    p.add_argument("--zones-cfg", default="config/zones.json")
    p.add_argument("--store-id", default="ST1008")
    args = p.parse_args()

    convert_file(args.input, args.output, args.zones_cfg, args.store_id)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
