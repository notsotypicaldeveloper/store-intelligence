"""
Zone enter/exit/dwell and billing queue events.

Uses point-in-polygon (ray casting) on the track's foot position to determine
which zone the person is currently standing in.

Emits:
  ZONE_ENTER  — when a track first enters a zone polygon
  ZONE_EXIT   — when a track leaves a zone polygon
  ZONE_DWELL  — every DWELL_INTERVAL_FRAMES of continuous presence,
                with cumulative dwell_ms in the event
  BILLING_QUEUE_JOIN    — on entering the billing zone (CASH_COUNTER / FOH)
  BILLING_QUEUE_ABANDON — on leaving the billing zone without a subsequent
                          POS transaction (flagged as candidate; API confirms)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

DWELL_INTERVAL_FRAMES = 450   # ~30 s at 15 fps
BILLING_ZONES = {"CASH_COUNTER", "FOH"}


@dataclass
class ZoneEvent:
    event_type: str      # ZONE_ENTER | ZONE_EXIT | ZONE_DWELL | BILLING_QUEUE_JOIN | BILLING_QUEUE_ABANDON
    visitor_id: str
    zone_id: str
    frame_idx: int
    camera_id: str
    dwell_ms: int = 0
    queue_depth: int = 0  # set for billing events
    confidence: float = 1.0
    low_confidence: bool = False


@dataclass
class _ZoneState:
    current_zone: Optional[str] = None
    zone_enter_frame: int = 0
    dwell_emit_frame: int = 0   # last frame we emitted a DWELL
    was_in_billing: bool = False


class ZoneTracker:
    """
    Per-camera zone tracker. Manages enter/exit/dwell lifecycle per visitor_id.
    fps is needed to convert frame counts → milliseconds for dwell_ms.
    """

    def __init__(self, camera_id: str, zone_polygons: list[dict], fps: float = 15.0):
        self.camera_id = camera_id
        self._polygons = zone_polygons   # list of {"zone_id": ..., "polygon": [[x,y],...]}
        self._fps = fps
        self._states: dict[str, _ZoneState] = {}
        self._billing_occupancy: int = 0   # current billing-zone occupant count

    def process_frame(
        self,
        visitor_id: str,
        foot_xy: tuple[float, float],
        frame_idx: int,
        confidence: float = 1.0,
        low_confidence: bool = False,
    ) -> list[ZoneEvent]:
        events: list[ZoneEvent] = []

        if visitor_id not in self._states:
            self._states[visitor_id] = _ZoneState()

        state = self._states[visitor_id]
        zone_id = self._point_in_zone(foot_xy)

        frames_to_ms = lambda f: int(f / self._fps * 1000)

        if zone_id != state.current_zone:
            # Zone exit (if was in one)
            if state.current_zone is not None:
                events.append(ZoneEvent(
                    event_type="ZONE_EXIT",
                    visitor_id=visitor_id,
                    zone_id=state.current_zone,
                    frame_idx=frame_idx,
                    camera_id=self.camera_id,
                    dwell_ms=frames_to_ms(frame_idx - state.zone_enter_frame),
                    confidence=confidence,
                    low_confidence=low_confidence,
                ))
                if state.current_zone in BILLING_ZONES:
                    self._billing_occupancy = max(0, self._billing_occupancy - 1)
                    events.append(ZoneEvent(
                        event_type="BILLING_QUEUE_ABANDON",
                        visitor_id=visitor_id,
                        zone_id=state.current_zone,
                        frame_idx=frame_idx,
                        camera_id=self.camera_id,
                        queue_depth=self._billing_occupancy,
                        confidence=confidence,
                    ))
                    state.was_in_billing = True

            # Zone enter
            if zone_id is not None:
                events.append(ZoneEvent(
                    event_type="ZONE_ENTER",
                    visitor_id=visitor_id,
                    zone_id=zone_id,
                    frame_idx=frame_idx,
                    camera_id=self.camera_id,
                    confidence=confidence,
                    low_confidence=low_confidence,
                ))
                if zone_id in BILLING_ZONES:
                    self._billing_occupancy += 1
                    events.append(ZoneEvent(
                        event_type="BILLING_QUEUE_JOIN",
                        visitor_id=visitor_id,
                        zone_id=zone_id,
                        frame_idx=frame_idx,
                        camera_id=self.camera_id,
                        queue_depth=self._billing_occupancy,
                        confidence=confidence,
                    ))

            state.current_zone = zone_id
            state.zone_enter_frame = frame_idx
            state.dwell_emit_frame = frame_idx

        else:
            # Continuous dwell — emit ZONE_DWELL every DWELL_INTERVAL_FRAMES
            if zone_id is not None:
                frames_in_zone = frame_idx - state.dwell_emit_frame
                if frames_in_zone >= DWELL_INTERVAL_FRAMES:
                    cumulative_ms = frames_to_ms(frame_idx - state.zone_enter_frame)
                    events.append(ZoneEvent(
                        event_type="ZONE_DWELL",
                        visitor_id=visitor_id,
                        zone_id=zone_id,
                        frame_idx=frame_idx,
                        camera_id=self.camera_id,
                        dwell_ms=cumulative_ms,
                        confidence=confidence,
                    ))
                    state.dwell_emit_frame = frame_idx

        return events

    def _point_in_zone(self, foot_xy: tuple[float, float]) -> Optional[str]:
        """Ray-casting point-in-polygon; returns zone_id of first match."""
        x, y = foot_xy
        for z in self._polygons:
            if _ray_cast(x, y, z["polygon"]):
                return z["zone_id"]
        return None


def _ray_cast(x: float, y: float, polygon: list[list[int]]) -> bool:
    """Standard ray-casting algorithm for point-in-polygon."""
    n = len(polygon)
    inside = False
    px, py = x, y
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside
