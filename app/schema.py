from enum import Enum
from typing import Optional, Any
from pydantic import BaseModel, Field, model_validator
import uuid


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


# Event types that must NOT carry a zone_id
_ZONELESS_TYPES = {EventType.ENTRY, EventType.EXIT, EventType.REENTRY}


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: Optional[int] = None

    model_config = {"extra": "allow"}


class Event(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: str  # ISO-8601 UTC, e.g. "2026-04-10T09:00:00Z"
    zone_id: Optional[str] = None
    dwell_ms: int = Field(default=0, ge=0)
    is_staff: bool = False
    confidence: float = Field(ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @model_validator(mode="after")
    def check_zone_consistency(self) -> "Event":
        if self.event_type in _ZONELESS_TYPES and self.zone_id is not None:
            raise ValueError(
                f"zone_id must be null for {self.event_type.value} events"
            )
        if self.event_type == EventType.ZONE_DWELL and self.dwell_ms == 0:
            raise ValueError("ZONE_DWELL events must have dwell_ms > 0")
        return self


# ── Ingest request / response ──────────────────────────────────────────────

class IngestRequest(BaseModel):
    events: list[Event] = Field(max_length=500)


class IngestError(BaseModel):
    event_id: Optional[str] = None
    index: int
    reason: str


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    errors: list[IngestError] = []


# ── Analytics response shapes (zero-state safe) ───────────────────────────

class MetricsResponse(BaseModel):
    store_id: str
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    unique_visitors: int = 0
    conversion_rate: float = 0.0
    avg_dwell_per_zone: dict[str, float] = {}
    queue_depth: int = 0
    abandonment_rate: float = 0.0


class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float = 0.0


class FunnelResponse(BaseModel):
    store_id: str
    stages: list[FunnelStage] = []


class HeatmapZone(BaseModel):
    zone_id: str
    visit_frequency: float = 0.0   # normalized 0–100
    avg_dwell_ms: float = 0.0
    data_confidence: bool = True    # False when <20 sessions


class HeatmapResponse(BaseModel):
    store_id: str
    zones: list[HeatmapZone] = []


class Anomaly(BaseModel):
    anomaly_type: str
    severity: str  # INFO | WARN | CRITICAL
    description: str
    suggested_action: str
    detected_at: str


class AnomaliesResponse(BaseModel):
    store_id: str
    anomalies: list[Anomaly] = []


class StoreHealth(BaseModel):
    store_id: str
    last_event_at: Optional[str] = None
    stale_feed: bool = False


class HealthResponse(BaseModel):
    status: str = "ok"
    stores: dict[str, StoreHealth] = {}
