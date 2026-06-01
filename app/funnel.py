import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db import get_db
from app.schema import FunnelResponse, FunnelStage

logger = logging.getLogger("store_intelligence")
router = APIRouter()


@router.get("/stores/{store_id}/funnel", response_model=FunnelResponse, tags=["analytics"])
def get_funnel(store_id: str):
    """
    Session-based conversion funnel: Entry → Zone Visit → Billing Queue → Purchase.
    REENTRY events collapse back to the original visitor_id — not double-counted.
    Staff excluded throughout.
    """
    try:
        with get_db() as conn:
            # Stage 1: distinct entrants (ENTRY only; staff excluded). REENTRY is
            # a returning visitor and must not inflate the funnel's top — the
            # Re-ID step already reused the original visitor_id.
            entry_count = conn.execute(
                """SELECT COUNT(DISTINCT visitor_id) as cnt
                   FROM events
                   WHERE store_id=? AND is_staff=0
                     AND event_type = 'ENTRY'""",
                (store_id,),
            ).fetchone()["cnt"]

            # Stage 2: visitors who entered at least one brand zone
            zone_count = conn.execute(
                """SELECT COUNT(DISTINCT e.visitor_id) as cnt
                   FROM events e
                   WHERE e.store_id=? AND e.is_staff=0
                     AND e.event_type = 'ZONE_ENTER'
                     AND e.visitor_id IN (
                         SELECT DISTINCT visitor_id FROM events
                         WHERE store_id=? AND is_staff=0
                           AND event_type = 'ENTRY'
                     )""",
                (store_id, store_id),
            ).fetchone()["cnt"]

            # Stage 3: visitors who joined the billing queue
            billing_count = conn.execute(
                """SELECT COUNT(DISTINCT e.visitor_id) as cnt
                   FROM events e
                   WHERE e.store_id=? AND e.is_staff=0
                     AND e.event_type = 'BILLING_QUEUE_JOIN'
                     AND e.visitor_id IN (
                         SELECT DISTINCT visitor_id FROM events
                         WHERE store_id=? AND is_staff=0
                           AND event_type = 'ENTRY'
                     )""",
                (store_id, store_id),
            ).fetchone()["cnt"]

            # Stage 4: purchased (time-window POS correlation)
            purchase_count = _get_purchase_count(conn, store_id)

    except Exception as exc:
        logger.error(f"funnel error: {exc}")
        return JSONResponse(status_code=503, content={"error": "storage_unavailable"})

    stages: list[FunnelStage] = []
    counts = [
        ("Entry", entry_count),
        ("Zone Visit", zone_count),
        ("Billing Queue", billing_count),
        ("Purchase", purchase_count),
    ]
    for i, (name, count) in enumerate(counts):
        prev = counts[i - 1][1] if i > 0 else count
        drop_off = round((1 - count / prev) * 100, 1) if prev > 0 else 0.0
        stages.append(FunnelStage(stage=name, count=count, drop_off_pct=drop_off if i > 0 else 0.0))

    return FunnelResponse(store_id=store_id, stages=stages)


def _get_purchase_count(conn, store_id: str) -> int:
    try:
        from app.conversion import get_converted_visitor_ids
        converted = get_converted_visitor_ids(conn, store_id)
        return len(converted)
    except Exception:
        return 0
