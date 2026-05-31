"""
Event construction and JSONL emission.

Builds a validated Event record from pipeline-internal types and appends it
to the output JSONL file. Validation happens at emit time — bad events raise
immediately rather than silently corrupting the output.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def frame_to_timestamp(
    clip_start: datetime,
    frame_idx: int,
    fps: float,
) -> str:
    """Convert frame index to ISO-8601 UTC timestamp string."""
    offset_secs = frame_idx / fps
    ts = clip_start + timedelta(seconds=offset_secs)
    return ts.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def build_event(
    store_id: str,
    camera_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: str,
    confidence: float,
    is_staff: bool = False,
    zone_id: Optional[str] = None,
    dwell_ms: int = 0,
    metadata: Optional[dict] = None,
    event_id: Optional[str] = None,
) -> dict:
    """Build and Pydantic-validate an event dict."""
    from app.schema import Event
    ev = Event(
        event_id=event_id or str(uuid.uuid4()),
        store_id=store_id,
        camera_id=camera_id,
        visitor_id=visitor_id,
        event_type=event_type,
        timestamp=timestamp,
        zone_id=zone_id,
        dwell_ms=dwell_ms,
        is_staff=is_staff,
        confidence=max(0.0, min(1.0, confidence)),
        metadata=metadata or {},
    )
    return ev.model_dump(mode="json")


class EventWriter:
    """Appends validated event dicts to a JSONL file."""

    def __init__(self, output_path: str | Path):
        self._path = Path(output_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._path, "a", encoding="utf-8")
        self._count = 0

    def write(self, event: dict) -> None:
        self._fh.write(json.dumps(event, default=str) + "\n")
        self._count += 1

    def flush(self) -> None:
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()
        logger.info("EventWriter closed: %s  events_written=%d", self._path, self._count)

    @property
    def count(self) -> int:
        return self._count

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
