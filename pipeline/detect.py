"""
YOLOv8 + ByteTrack per-camera detection → stable tracks.

Emits per-frame track records:
  (track_id, bbox_xyxy, confidence, frame_idx, camera_id, foot_xy)

Low-confidence detections (below CONF_WARN) are flagged but never dropped.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

CONF_WARN = 0.40   # detections below this are tagged low-confidence
PERSON_CLASS = 0   # COCO class index for "person"


@dataclass
class TrackRecord:
    track_id: int
    bbox_xyxy: tuple[float, float, float, float]   # x1, y1, x2, y2
    confidence: float
    frame_idx: int
    camera_id: str
    low_confidence: bool = False

    @property
    def foot_xy(self) -> tuple[float, float]:
        """Bottom-centre of bbox — used for zone point-in-polygon."""
        x1, y1, x2, y2 = self.bbox_xyxy
        return ((x1 + x2) / 2, y2)

    @property
    def centroid(self) -> tuple[float, float]:
        x1, y1, x2, y2 = self.bbox_xyxy
        return ((x1 + x2) / 2, (y1 + y2) / 2)


def iter_tracks(
    clip_path: str | Path,
    camera_id: str,
    frame_skip: int = 1,
) -> Generator[tuple, None, None]:
    """
    Yield (frame, [TrackRecord, ...]) per processed frame. The raw frame is
    yielded so callers can compute appearance signatures (re-entry Re-ID,
    staff uniform colour) without re-decoding the clip.
    Requires ultralytics + opencv-python-headless (available in Docker).

    frame_skip=1 → every frame; frame_skip=2 → every other frame, etc.
    """
    try:
        import cv2
        from ultralytics import YOLO
    except ImportError as exc:
        raise RuntimeError(
            "Detection dependencies missing. Run inside Docker: "
            "pip install ultralytics opencv-python-headless"
        ) from exc

    model = YOLO("yolov8s.pt")   # downloads on first use; cached thereafter
    cap = cv2.VideoCapture(str(clip_path))
    if not cap.isOpened():
        raise FileNotFoundError(f"Cannot open clip: {clip_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    frame_idx = 0
    next_log_pct = 10   # next progress milestone to log

    logger.info(
        "Starting detection: %s  camera=%s  fps=%.1f  frames=%d",
        clip_path, camera_id, fps, total_frames,
    )

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                break

            if frame_idx % frame_skip != 0:
                frame_idx += 1
                continue

            # ByteTrack integrated via ultralytics tracker
            results = model.track(
                frame,
                persist=True,
                tracker="bytetrack.yaml",
                classes=[PERSON_CLASS],
                verbose=False,
            )

            records: list[TrackRecord] = []
            if results and results[0].boxes is not None:
                boxes = results[0].boxes
                for box in boxes:
                    tid = int(box.id.item()) if box.id is not None else -1
                    conf = float(box.conf.item())
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    records.append(TrackRecord(
                        track_id=tid,
                        bbox_xyxy=(x1, y1, x2, y2),
                        confidence=conf,
                        frame_idx=frame_idx,
                        camera_id=camera_id,
                        low_confidence=(conf < CONF_WARN),
                    ))

            yield frame, records
            frame_idx += 1

            # Periodic progress so long clips don't look hung on CPU
            while total_frames and frame_idx * 100 >= next_log_pct * total_frames and next_log_pct <= 100:
                logger.info(
                    "  %s detection %d%% (%d/%d frames)",
                    camera_id, next_log_pct, frame_idx, total_frames,
                )
                next_log_pct += 10
    finally:
        cap.release()
