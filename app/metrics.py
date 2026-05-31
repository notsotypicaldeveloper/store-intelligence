import json
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db import get_db
from app.schema import MetricsResponse

logger = logging.getLogger("store_intelligence")
router = APIRouter()


@router.get("/stores/{store_id}/metrics", response_model=MetricsResponse, tags=["analytics"])
def get_metrics(store_id: str):
    try:
        with get_db() as conn:
            row = conn.execute(
                """SELECT COUNT(DISTINCT visitor_id) as cnt
                   FROM events
                   WHERE store_id=? AND is_staff=0
                     AND event_type IN ('ENTRY','REENTRY')""",
                (store_id,),
            ).fetchone()
            unique_visitors = row["cnt"] if row else 0

            zone_rows = conn.execute(
                """SELECT zone_id, AVG(dwell_ms) as avg_dwell
                   FROM events
                   WHERE store_id=? AND is_staff=0 AND event_type='ZONE_DWELL'
                     AND zone_id IS NOT NULL
                   GROUP BY zone_id""",
                (store_id,),
            ).fetchall()
            avg_dwell_per_zone = {r["zone_id"]: round(r["avg_dwell"], 2) for r in zone_rows}

            queue_row = conn.execute(
                """SELECT metadata FROM events
                   WHERE store_id=? AND event_type='BILLING_QUEUE_JOIN'
                   ORDER BY timestamp DESC LIMIT 1""",
                (store_id,),
            ).fetchone()
            queue_depth = 0
            if queue_row:
                try:
                    meta = json.loads(queue_row["metadata"] or "{}")
                    queue_depth = meta.get("queue_depth", 0) or 0
                except Exception:
                    pass

            joins = conn.execute(
                "SELECT COUNT(*) as cnt FROM events WHERE store_id=? AND event_type='BILLING_QUEUE_JOIN' AND is_staff=0",
                (store_id,),
            ).fetchone()["cnt"]
            abandons = conn.execute(
                "SELECT COUNT(*) as cnt FROM events WHERE store_id=? AND event_type='BILLING_QUEUE_ABANDON' AND is_staff=0",
                (store_id,),
            ).fetchone()["cnt"]
            abandonment_rate = round(abandons / joins, 4) if joins > 0 else 0.0

            conversion_rate = _get_conversion_rate(conn, store_id)

    except Exception as exc:
        logger.error(f"metrics error: {exc}")
        return JSONResponse(status_code=503, content={"error": "storage_unavailable"})

    return MetricsResponse(
        store_id=store_id,
        unique_visitors=unique_visitors,
        conversion_rate=conversion_rate,
        avg_dwell_per_zone=avg_dwell_per_zone,
        queue_depth=queue_depth,
        abandonment_rate=abandonment_rate,
    )


def _get_conversion_rate(conn, store_id: str) -> float:
    """Inline conversion rate calculation (avoids circular import with conversion module)."""
    try:
        from app.conversion import compute_conversion_rate
        return compute_conversion_rate(conn, store_id)
    except Exception:
        return 0.0
