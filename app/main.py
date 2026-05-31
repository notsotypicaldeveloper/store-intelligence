import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.db import init_db
from app.ingestion import router as ingest_router
from app.metrics import router as metrics_router
from app.funnel import router as funnel_router
from app.heatmap import router as heatmap_router
from app.anomalies import router as anomalies_router
from app.health import router as health_router

logger = logging.getLogger("store_intelligence")
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":%(message)s}',
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Store Intelligence API", version="0.1.0", lifespan=lifespan)


# ── Structured request logging middleware ─────────────────────────────────

@app.middleware("http")
async def log_requests(request: Request, call_next):
    trace_id = str(uuid.uuid4())
    request.state.trace_id = trace_id
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as exc:
        logger.error(json.dumps({
            "trace_id": trace_id,
            "endpoint": str(request.url.path),
            "error": str(exc),
        }))
        return JSONResponse(status_code=500, content={"error": "internal_server_error"})
    latency_ms = round((time.monotonic() - start) * 1000, 2)
    store_id = request.path_params.get("store_id", "")
    event_count = ""
    logger.info(json.dumps({
        "trace_id": trace_id,
        "store_id": store_id,
        "endpoint": str(request.url.path),
        "method": request.method,
        "status_code": response.status_code,
        "latency_ms": latency_ms,
    }))
    return response


# ── Global error handler — no raw stack traces ────────────────────────────

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(json.dumps({"endpoint": str(request.url.path), "error": repr(exc)}))
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "detail": str(exc)},
    )


# ── Routers ───────────────────────────────────────────────────────────────

app.include_router(health_router)
app.include_router(ingest_router)
app.include_router(metrics_router)
app.include_router(funnel_router)
app.include_router(heatmap_router)
app.include_router(anomalies_router)
