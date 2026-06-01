"""
Staff exclusion heuristic — flags visitors as is_staff=True.

Two complementary signals (both must be above threshold, or either alone
if strong enough):

1. Uniform-colour signature: a narrow colour histogram of the torso crop
   matched against a learned "staff uniform" palette.

2. Behavioural signals: persistent multi-zone movement, long cumulative
   dwell, or repeated billing-zone presence over the session.

Events are NEVER dropped — is_staff flag is set and query-time filtering
handles exclusion (R4). Explicitly framed as a heuristic; see CHOICES.md.

Known limits (documented):
- Staff who wear non-uniform colours on break will be missed.
- Customers who stand near the cash counter for a long time can be
  false-positively flagged.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)

# Thresholds — configurable
UNIFORM_SIM_THRESHOLD = 0.80      # colour similarity to staff palette to flag
MULTI_ZONE_THRESHOLD = 4          # distinct zones visited → probable staff
LONG_DWELL_THRESHOLD_FRAMES = 900  # ~60 s at 15 fps in one zone
BILLING_REPEAT_THRESHOLD = 3       # billing-zone entries in session


@dataclass
class _VisitorProfile:
    zones_visited: set = field(default_factory=set)
    billing_visits: int = 0
    max_zone_dwell_frames: int = 0
    current_zone: str | None = None
    current_zone_frames: int = 0
    colour_sig: np.ndarray | None = None   # first captured signature
    is_staff: bool = False


class StaffClassifier:
    """
    Per-camera stateful classifier. Call update() every processed frame,
    then query is_staff(visitor_id) at session boundary.
    """

    def __init__(self, staff_colour_palette: list[np.ndarray] | None = None):
        # staff_colour_palette: list of pre-computed unit histogram vectors
        # representing known staff uniform colours. Empty list = colour
        # signal disabled (behavioural only).
        self._palette = staff_colour_palette or []
        self._profiles: dict[str, _VisitorProfile] = defaultdict(_VisitorProfile)

    def update(
        self,
        visitor_id: str,
        zone_id: str | None,
        frame: np.ndarray | None,
        bbox_xyxy: tuple | None,
    ) -> None:
        """Update the visitor profile with the latest frame information."""
        profile = self._profiles[visitor_id]

        # Capture colour signature on first sighting
        if profile.colour_sig is None and frame is not None and bbox_xyxy is not None:
            profile.colour_sig = _torso_signature(frame, bbox_xyxy)

        # Zone tracking
        if zone_id:
            profile.zones_visited.add(zone_id)
            if zone_id == profile.current_zone:
                profile.current_zone_frames += 1
            else:
                if profile.current_zone_frames > profile.max_zone_dwell_frames:
                    profile.max_zone_dwell_frames = profile.current_zone_frames
                profile.current_zone = zone_id
                profile.current_zone_frames = 1

            if zone_id == "CASH_COUNTER":
                profile.billing_visits += 1

        # Real-time staff classification
        profile.is_staff = self._classify(profile)

    def is_staff(self, visitor_id: str) -> bool:
        return self._profiles[visitor_id].is_staff

    def _classify(self, profile: _VisitorProfile) -> bool:
        # Colour match against uniform palette
        colour_match = False
        if self._palette and profile.colour_sig is not None:
            best = max(_cosine_sim(profile.colour_sig, p) for p in self._palette)
            colour_match = best >= UNIFORM_SIM_THRESHOLD

        # Behavioural signals
        multi_zone = len(profile.zones_visited) >= MULTI_ZONE_THRESHOLD
        long_dwell = profile.max_zone_dwell_frames >= LONG_DWELL_THRESHOLD_FRAMES
        repeated_billing = profile.billing_visits >= BILLING_REPEAT_THRESHOLD

        strong_behavioural = multi_zone and (long_dwell or repeated_billing)
        return colour_match or strong_behavioural


def _torso_signature(frame: np.ndarray, bbox_xyxy: tuple) -> np.ndarray | None:
    """Colour histogram over the upper-half of the bbox (torso region)."""
    try:
        import cv2
        x1, y1, x2, y2 = [int(v) for v in bbox_xyxy]
        mid_y = y1 + (y2 - y1) // 2
        crop = frame[y1:mid_y, x1:x2]
        if crop.size == 0:
            return None
        hists = [cv2.calcHist([crop], [c], None, [16], [0, 256]).flatten()
                 for c in range(3)]
        vec = np.concatenate(hists).astype(np.float32)
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec
    except Exception:
        return None


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))
