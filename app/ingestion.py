import json
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.db import get_db
from app.schema import IngestError, IngestRequest, IngestResponse

logger = logging.getLogger("store_intelligence")
router = APIRouter()


@router.post("/events/ingest", response_model=IngestResponse, tags=["ingest"])
def ingest_events(payload: IngestRequest):
    accepted = 0
    errors: list[IngestError] = []

    try:
        with get_db() as conn:
            for i, event in enumerate(payload.events):
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO events
                           (event_id, store_id, camera_id, visitor_id, event_type,
                            timestamp, zone_id, dwell_ms, is_staff, confidence, metadata)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            event.event_id,
                            event.store_id,
                            event.camera_id,
                            event.visitor_id,
                            event.event_type.value,
                            event.timestamp,
                            event.zone_id,
                            event.dwell_ms,
                            int(event.is_staff),
                            event.confidence,
                            event.metadata.model_dump_json(),
                        ),
                    )
                    accepted += 1
                except Exception as exc:
                    errors.append(IngestError(
                        event_id=event.event_id, index=i, reason=str(exc)
                    ))
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"error": "storage_unavailable", "detail": "database write failed"},
        )

    logger.info(json.dumps({
        "event": "ingest",
        "total": len(payload.events),
        "accepted": accepted,
        "rejected": len(errors),
    }))
    return IngestResponse(accepted=accepted, rejected=len(errors), errors=errors)
