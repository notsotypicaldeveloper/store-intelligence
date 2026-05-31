import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db import get_db
from app.schema import HealthResponse, StoreHealth

logger = logging.getLogger("store_intelligence")
router = APIRouter()

_STALE_THRESHOLD_MINUTES = 10


@router.get("/health", response_model=HealthResponse, tags=["ops"])
def health():
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT store_id, MAX(timestamp) as last_ts FROM events GROUP BY store_id"
            ).fetchall()
    except Exception:
        return JSONResponse(status_code=503, content={"error": "storage_unavailable"})

    now = datetime.now(timezone.utc)
    stores: dict[str, StoreHealth] = {}
    for row in rows:
        try:
            last_ts = datetime.fromisoformat(row["last_ts"].replace("Z", "+00:00"))
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            stale = (now - last_ts) > timedelta(minutes=_STALE_THRESHOLD_MINUTES)
        except Exception:
            stale = True
            last_ts = None
        stores[row["store_id"]] = StoreHealth(
            store_id=row["store_id"],
            last_event_at=row["last_ts"],
            stale_feed=stale,
        )
    return HealthResponse(status="ok", stores=stores)
