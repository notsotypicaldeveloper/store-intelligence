"""
Re-entry de-duplication via appearance signature + temporal gating.

When a new inbound crossing occurs, we compute a lightweight appearance
signature (colour histogram over the torso region) for the entering person
and compare it against recently exited visitors within a configurable time
window. A match → REENTRY reusing the prior visitor_id; no match → new ENTRY.

Known limitation (documented in CHOICES.md): two different visitors with
near-identical clothing crossing within seconds can be mis-linked. The
configurable time window and similarity threshold bound this failure mode.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

REENTRY_WINDOW_SECS = 300   # 5 minutes — configurable
SIMILARITY_THRESHOLD = 0.85  # cosine similarity floor for a match

# Use only 3×16-bin colour histogram for speed; faces are blurred so we
# use the full bbox (torso + legs) which is more clothing-representative.
_HIST_BINS = 16


@dataclass
class _ExitRecord:
    visitor_id: str
    signature: np.ndarray     # colour histogram vector
    exited_at: float          # time.monotonic()


class ReIDTracker:
    """
    Maintains a rolling buffer of recently-exited visitors (within REENTRY_WINDOW_SECS).
    On each new ENTRY crossing, checks whether the entering person's appearance
    matches a recently exited visitor.
    """

    def __init__(self, window_secs: int = REENTRY_WINDOW_SECS,
                 threshold: float = SIMILARITY_THRESHOLD):
        self._window = window_secs
        self._threshold = threshold
        self._exits: list[_ExitRecord] = []

    def record_exit(
        self, visitor_id: str, frame: np.ndarray, bbox_xyxy: tuple,
        now: Optional[float] = None,
    ) -> None:
        # `now` is the simulated clip time (seconds) when running over a video
        # file; falls back to wall-clock for unit tests / live use.
        now = time.monotonic() if now is None else now
        sig = _compute_signature(frame, bbox_xyxy)
        if sig is not None:
            self._exits.append(_ExitRecord(
                visitor_id=visitor_id,
                signature=sig,
                exited_at=now,
            ))
        self._prune(now)

    def check_reentry(
        self, frame: np.ndarray, bbox_xyxy: tuple, now: Optional[float] = None
    ) -> Optional[str]:
        """
        Return a prior visitor_id if the entering person matches a recent exit,
        else None (new ENTRY).
        """
        now = time.monotonic() if now is None else now
        self._prune(now)
        sig = _compute_signature(frame, bbox_xyxy)
        if sig is None:
            return None

        best_score = 0.0
        best_vid: Optional[str] = None
        for rec in self._exits:
            score = _cosine_sim(sig, rec.signature)
            if score > best_score:
                best_score = score
                best_vid = rec.visitor_id

        if best_score >= self._threshold:
            logger.debug("Re-ID match visitor=%s score=%.3f", best_vid, best_score)
            return best_vid
        return None

    def _prune(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        self._exits = [r for r in self._exits if now - r.exited_at <= self._window]


def _compute_signature(frame: np.ndarray, bbox_xyxy: tuple) -> Optional[np.ndarray]:
    """Compute a normalised RGB colour histogram over the bbox crop."""
    try:
        import cv2
        x1, y1, x2, y2 = [int(v) for v in bbox_xyxy]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(frame.shape[1], x2), min(frame.shape[0], y2)
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        hists = []
        for ch in range(3):
            h = cv2.calcHist([crop], [ch], None, [_HIST_BINS], [0, 256])
            hists.append(h.flatten())
        vec = np.concatenate(hists).astype(np.float32)
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec
    except Exception:
        return None


def _cosine_sim(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))  # both are already unit-normalised
