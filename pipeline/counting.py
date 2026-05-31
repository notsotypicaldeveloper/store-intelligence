"""
Line-crossing direction detection → ENTRY / EXIT events.

Only the camera with role="entry" emits ENTRY/EXIT events; all other cameras
are silent on line-crossing (prevents double-counting across camera overlaps).

Each new inbound track opens a visitor session (fresh visitor_id).
N tracks crossing inbound within the same frame cluster → N ENTRY events
(group entry handled naturally by ByteTrack returning N distinct track_ids).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from pipeline.detect import TrackRecord

logger = logging.getLogger(__name__)

# Hysteresis band: a track must cross the line by this many pixels AND hold
# direction for MIN_CROSS_FRAMES frames before we emit an event.
MIN_CROSS_FRAMES = 2
HYSTERESIS_PX = 8


@dataclass
class _TrackState:
    last_y: float
    side: int            # +1 = above line (outside), -1 = below line (inside)
    frames_on_side: int = 0
    visitor_id: Optional[str] = None
    has_entered: bool = False


@dataclass
class CountingResult:
    event_type: str          # "ENTRY" | "EXIT"
    visitor_id: str
    track_id: int
    frame_idx: int
    camera_id: str
    confidence: float
    low_confidence: bool


class LineCrossingCounter:
    """
    Stateful counter for one camera's entry line.

    entry_line: [[x1, y1], [x2, y2]] — horizontal or near-horizontal segment.
    For simplicity we use the average Y of the two endpoints as the threshold.
    Above threshold (smaller y) = outside/street side.
    Below threshold (larger y) = inside the store.

    Inbound crossing (outside→inside, i.e. y increases past threshold) = ENTRY.
    Outbound crossing (inside→outside, y decreases past threshold) = EXIT.
    """

    def __init__(self, camera_id: str, entry_line: list[list[int]]):
        self.camera_id = camera_id
        self._line_y = (entry_line[0][1] + entry_line[1][1]) / 2
        self._states: dict[int, _TrackState] = {}
        # visitor session registry: track_id → visitor_id (for this camera)
        self._session: dict[int, str] = {}

    def process_frame(self, records: list[TrackRecord]) -> list[CountingResult]:
        events: list[CountingResult] = []
        seen_ids = set()

        for rec in records:
            tid = rec.track_id
            if tid < 0:
                continue
            seen_ids.add(tid)
            cx, cy = rec.centroid
            line_y = self._line_y

            if tid not in self._states:
                side = 1 if cy < line_y else -1
                self._states[tid] = _TrackState(last_y=cy, side=side)
                continue

            state = self._states[tid]
            new_side = 1 if cy < line_y else -1

            if new_side == state.side:
                state.frames_on_side += 1
                state.last_y = cy
                continue

            # Side changed — check hysteresis
            delta = abs(cy - line_y)
            if delta < HYSTERESIS_PX:
                continue  # too close to the line, ignore

            if state.frames_on_side >= MIN_CROSS_FRAMES:
                if new_side == -1:  # crossed inbound (outside→inside)
                    vid = str(uuid.uuid4()).replace("-", "")[:12]
                    vid = f"VIS_{vid}"
                    self._session[tid] = vid
                    events.append(CountingResult(
                        event_type="ENTRY",
                        visitor_id=vid,
                        track_id=tid,
                        frame_idx=rec.frame_idx,
                        camera_id=self.camera_id,
                        confidence=rec.confidence,
                        low_confidence=rec.low_confidence,
                    ))
                    state.has_entered = True
                else:  # crossed outbound (inside→outside)
                    vid = self._session.get(tid, f"VIS_{tid:08x}")
                    events.append(CountingResult(
                        event_type="EXIT",
                        visitor_id=vid,
                        track_id=tid,
                        frame_idx=rec.frame_idx,
                        camera_id=self.camera_id,
                        confidence=rec.confidence,
                        low_confidence=rec.low_confidence,
                    ))

            state.side = new_side
            state.frames_on_side = 1
            state.last_y = cy

        # Prune tracks no longer visible
        stale = set(self._states) - seen_ids
        for tid in stale:
            del self._states[tid]

        return events

    def get_session(self, track_id: int) -> Optional[str]:
        return self._session.get(track_id)
