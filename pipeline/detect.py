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
) -> Generator[list[TrackRecord], None, None]:
    """
    Yield one list of TrackRecords per processed frame.
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
    frame_idx = 0

    logger.info("Starting detection: %s  camera=%s  fps=%.1f", clip_path, camera_id, fps)

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

            yield records
            frame_idx += 1
    finally:
        cap.release()
