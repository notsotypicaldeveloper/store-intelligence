#!/usr/bin/env python3
"""
Camera calibration helper — extract representative frames from each clip,
then overlay zone polygons and entry line for visual verification.

Usage:
    # Extract one frame per clip into data/_frames/
    python pipeline/calibrate.py --extract

    # Overlay configured zones on frame for visual check
    python pipeline/calibrate.py --overlay --camera CAM_ENTRY

Requirements: opencv-python-headless (available inside Docker)
"""
import argparse
import json
import os
import sys
from pathlib import Path

CLIPS_DIR = Path("data/clips")
FRAMES_DIR = Path("data/_frames")
CAMERAS_CFG = Path("config/cameras.json")
ZONES_CFG = Path("config/zones.json")


def extract_frames():
    """Extract one frame at 10 s from each clip using OpenCV."""
    try:
        import cv2
    except ImportError:
        print("ERROR: opencv-python-headless not installed. Run inside Docker.", file=sys.stderr)
        return

    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    clips = sorted(CLIPS_DIR.glob("CAM*.mp4"))
    if not clips:
        print(f"No clips found in {CLIPS_DIR}")
        return

    for clip in clips:
        cap = cv2.VideoCapture(str(clip))
        fps = cap.get(cv2.CAP_PROP_FPS) or 15
        target_frame = int(fps * 10)  # 10 s mark
        cap.set(cv2.CAP_PROP_POS_FRAMES, target_frame)
        ret, frame = cap.read()
        cap.release()
        if ret:
            out = FRAMES_DIR / f"{clip.stem}.jpg"
            cv2.imwrite(str(out), frame)
            h, w = frame.shape[:2]
            print(f"  {clip.name} → {out}  ({w}×{h})")
        else:
            print(f"  {clip.name} → failed to read frame")


def overlay_zones(camera_id: str):
    """Draw entry line + zone polygons on the extracted frame."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        print("ERROR: opencv-python-headless required.", file=sys.stderr)
        return

    cameras = json.loads(CAMERAS_CFG.read_text())
    zones_cfg = json.loads(ZONES_CFG.read_text())

    cam = next((c for c in cameras["cameras"] if c["camera_id"] == camera_id), None)
    if cam is None:
        print(f"Camera {camera_id!r} not found in {CAMERAS_CFG}")
        return

    stem = Path(cam["clip_filename"]).stem
    frame_path = FRAMES_DIR / f"{stem}.jpg"
    if not frame_path.exists():
        print(f"Frame not found: {frame_path}. Run --extract first.")
        return

    img = cv2.imread(str(frame_path))

    # Draw entry line
    if "entry_line" in cam:
        p1, p2 = cam["entry_line"]
        cv2.line(img, tuple(p1), tuple(p2), (0, 0, 255), 3)
        cv2.putText(img, "ENTRY LINE", (p1[0], p1[1] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

    # Draw zones for this camera
    cam_zones = [z for z in zones_cfg.get("zones", []) if z.get("camera_id") == camera_id]
    colors = [(255, 0, 0), (0, 255, 0), (255, 165, 0), (0, 255, 255), (255, 0, 255)]
    for i, zone in enumerate(cam_zones):
        pts = np.array(zone["polygon"], dtype=np.int32).reshape((-1, 1, 2))
        color = colors[i % len(colors)]
        cv2.polylines(img, [pts], True, color, 2)
        cx = int(sum(p[0] for p in zone["polygon"]) / len(zone["polygon"]))
        cy = int(sum(p[1] for p in zone["polygon"]) / len(zone["polygon"]))
        cv2.putText(img, zone["zone_id"], (cx, cy),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

    out = FRAMES_DIR / f"{stem}_overlay.jpg"
    cv2.imwrite(str(out), img)
    print(f"Overlay written to {out}")


def main():
    parser = argparse.ArgumentParser(description="Camera calibration helper")
    parser.add_argument("--extract", action="store_true", help="Extract frames from clips")
    parser.add_argument("--overlay", action="store_true", help="Draw zones on extracted frame")
    parser.add_argument("--camera", default="CAM_ENTRY", help="Camera ID for overlay")
    args = parser.parse_args()

    if args.extract:
        print("Extracting frames …")
        extract_frames()
    if args.overlay:
        print(f"Overlaying zones for {args.camera} …")
        overlay_zones(args.camera)
    if not args.extract and not args.overlay:
        parser.print_help()


if __name__ == "__main__":
    main()
