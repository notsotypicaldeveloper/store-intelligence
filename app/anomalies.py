import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db import get_db
from app.schema import Anomaly, AnomaliesResponse

logger = logging.getLogger("store_intelligence")
router = APIRouter()

_QUEUE_SPIKE_THRESHOLD = 5   # queue depth ≥ this → CRITICAL
_QUEUE_WARN_THRESHOLD = 3    # queue depth ≥ this → WARN
_DEAD_ZONE_MINUTES = 30      # no visits in this window → dead zone anomaly
_CONVERSION_DROP_THRESHOLD = 0.30  # relative drop vs baseline triggers anomaly


@router.get("/stores/{store_id}/anomalies", response_model=AnomaliesResponse, tags=["analytics"])
def get_anomalies(store_id: str):
    now = datetime.now(timezone.utc)
    anomalies: list[Anomaly] = []

    try:
        with get_db() as conn:
            anomalies += _check_queue_spike(conn, store_id, now)
            anomalies += _check_dead_zones(conn, store_id, now)
            anomalies += _check_conversion_drop(conn, store_id, now)
    except Exception as exc:
        logger.error(f"anomalies error: {exc}")
        return JSONResponse(status_code=503, content={"error": "storage_unavailable"})

    return AnomaliesResponse(store_id=store_id, anomalies=anomalies)


def _check_queue_spike(conn, store_id: str, now: datetime) -> list[Anomaly]:
    row = conn.execute(
        """SELECT metadata, timestamp FROM events
           WHERE store_id=? AND event_type='BILLING_QUEUE_JOIN'
           ORDER BY timestamp DESC LIMIT 1""",
        (store_id,),
    ).fetchone()
    if not row:
        return []
    try:
        depth = json.loads(row["metadata"] or "{}").get("queue_depth", 0) or 0
    except Exception:
        return []

    if depth >= _QUEUE_SPIKE_THRESHOLD:
        return [Anomaly(
            anomaly_type="QUEUE_SPIKE",
            severity="CRITICAL",
            description=f"Billing queue depth is {depth} (threshold: {_QUEUE_SPIKE_THRESHOLD})",
            suggested_action="Deploy additional staff to the cash counter immediately.",
            detected_at=now.isoformat(),
        )]
    if depth >= _QUEUE_WARN_THRESHOLD:
        return [Anomaly(
            anomaly_type="QUEUE_SPIKE",
            severity="WARN",
            description=f"Billing queue depth is {depth} (warn threshold: {_QUEUE_WARN_THRESHOLD})",
            suggested_action="Consider opening a second billing station or redirecting customers.",
            detected_at=now.isoformat(),
        )]
    return []


def _check_dead_zones(conn, store_id: str, now: datetime) -> list[Anomaly]:
    cutoff = (now - timedelta(minutes=_DEAD_ZONE_MINUTES)).isoformat()

    # Zones that have EVER had visits
    ever_visited = {
        r["zone_id"]
        for r in conn.execute(
            "SELECT DISTINCT zone_id FROM events WHERE store_id=? AND zone_id IS NOT NULL AND event_type='ZONE_ENTER'",
            (store_id,),
        ).fetchall()
    }
    if not ever_visited:
        return []

    # Zones visited RECENTLY (within window)
    recently_visited = {
        r["zone_id"]
        for r in conn.execute(
            """SELECT DISTINCT zone_id FROM events
               WHERE store_id=? AND zone_id IS NOT NULL
                 AND event_type='ZONE_ENTER' AND timestamp >= ?""",
            (store_id, cutoff),
        ).fetchall()
    }

    dead = ever_visited - recently_visited
    anomalies = []
    for zone in sorted(dead):
        anomalies.append(Anomaly(
            anomaly_type="DEAD_ZONE",
            severity="INFO",
            description=f"Zone '{zone}' had no visitors in the last {_DEAD_ZONE_MINUTES} minutes.",
            suggested_action=f"Check if '{zone}' signage/lighting needs attention or restock display.",
            detected_at=now.isoformat(),
        ))
    return anomalies


def _check_conversion_drop(conn, store_id: str, now: datetime) -> list[Anomaly]:
    """
    Compare recent 2-hour conversion rate vs earlier-in-day baseline.
    With a single day of data we use an intra-day rolling baseline, documented honestly.
    """
    try:
        from app.conversion import compute_conversion_rate
        # Full-day rate as baseline
        baseline = compute_conversion_rate(conn, store_id)
        if baseline == 0.0:
            return []

        # Recent 2-hour window — compute via a narrowed query
        cutoff = (now - timedelta(hours=2)).isoformat()
        recent_visitors = conn.execute(
            """SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
               WHERE store_id=? AND is_staff=0 AND event_type IN ('ENTRY','REENTRY')
                 AND timestamp >= ?""",
            (store_id, cutoff),
        ).fetchone()["cnt"]

        if recent_visitors < 5:  # not enough data for a reliable recent rate
            return []

        # Simplified: if recent billing queue joins are proportionally fewer → flag
        recent_billing = conn.execute(
            """SELECT COUNT(DISTINCT visitor_id) as cnt FROM events
               WHERE store_id=? AND is_staff=0 AND event_type='BILLING_QUEUE_JOIN'
                 AND timestamp >= ?""",
            (store_id, cutoff),
        ).fetchone()["cnt"]
        recent_rate = recent_billing / recent_visitors if recent_visitors > 0 else 0.0
        if baseline > 0 and recent_rate < baseline * (1 - _CONVERSION_DROP_THRESHOLD):
            return [Anomaly(
                anomaly_type="CONVERSION_DROP",
                severity="WARN",
                description=(
                    f"Recent 2h conversion proxy ({recent_rate:.1%}) is "
                    f">{_CONVERSION_DROP_THRESHOLD:.0%} below day baseline ({baseline:.1%})."
                ),
                suggested_action="Review brand-zone staffing and check if promotional displays are active.",
                detected_at=now.isoformat(),
            )]
    except Exception:
        pass
    return []
