import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db import get_db
from app.schema import HeatmapResponse, HeatmapZone

logger = logging.getLogger("store_intelligence")
router = APIRouter()

_MIN_SESSIONS_FOR_CONFIDENCE = 20


@router.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse, tags=["analytics"])
def get_heatmap(store_id: str):
    """
    Zone visit frequency + avg dwell, normalized 0–100.
    data_confidence=False when fewer than 20 sessions in the window.
    """
    try:
        with get_db() as conn:
            # Total unique customer sessions
            total_sessions = conn.execute(
                """SELECT COUNT(DISTINCT visitor_id) as cnt
                   FROM events WHERE store_id=? AND is_staff=0
                     AND event_type IN ('ENTRY','REENTRY')""",
                (store_id,),
            ).fetchone()["cnt"]

            # Per-zone: distinct visitor counts + avg dwell
            zone_rows = conn.execute(
                """SELECT
                     zone_id,
                     COUNT(DISTINCT visitor_id) as visit_count,
                     AVG(CASE WHEN event_type='ZONE_DWELL' THEN dwell_ms ELSE NULL END) as avg_dwell
                   FROM events
                   WHERE store_id=? AND is_staff=0
                     AND event_type IN ('ZONE_ENTER','ZONE_DWELL')
                     AND zone_id IS NOT NULL
                   GROUP BY zone_id""",
                (store_id,),
            ).fetchall()

    except Exception as exc:
        logger.error(f"heatmap error: {exc}")
        return JSONResponse(status_code=503, content={"error": "storage_unavailable"})

    if not zone_rows:
        return HeatmapResponse(store_id=store_id, zones=[])

    # Normalize visit counts 0–100
    max_visits = max(r["visit_count"] for r in zone_rows) or 1
    data_confident = total_sessions >= _MIN_SESSIONS_FOR_CONFIDENCE

    zones = [
        HeatmapZone(
            zone_id=r["zone_id"],
            visit_frequency=round(r["visit_count"] / max_visits * 100, 1),
            avg_dwell_ms=round(r["avg_dwell"] or 0.0, 2),
            data_confidence=data_confident,
        )
        for r in zone_rows
    ]
    return HeatmapResponse(store_id=store_id, zones=zones)
