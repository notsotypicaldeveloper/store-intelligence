# Store Intelligence — Architecture & Design

Brigade Road, Bangalore · Single store · 4 cameras · Batch + replay pipeline

---

## Problem statement

Physical stores are an analytics blind spot: real-time visibility online, near-zero insight into footfall, dwell, conversion, or queue lengths in-store. This system converts raw CCTV from one real Purplle store (Brigade Road, 4 cameras) into trustworthy, queryable store analytics — without the de-dup and staff-counting errors that inflate vendor numbers.

The unit of truth is the **visitor session**. The north-star metric is **offline conversion rate**: purchasing visitors ÷ unique visitors (staff excluded, re-entries collapsed).

---

## Architecture overview

The four feeds map to roles by name (no `CAM 1`–`CAM 5` ↔ role assumption): `CAM 3 - entry.mp4` → entry/exit, `CAM 1 - zone.mp4` + `CAM 2 - zone.mp4` → shelf/aisle dwell, `CAM 5 - billing.mp4` → billing queue. There is no CAM 4. All feeds are 1920×1080. Files live directly in `data/` (no `clips/` subfolder).

```
data/*.mp4   (CAM 1/2/3/5)
        │
        ▼
┌─────────────────────────────────────────┐
│  Detection pipeline  (batch, offline)   │
│                                         │
│  detect.py  ── YOLOv8s + ByteTrack      │
│      │           per-camera tracks       │
│  counting.py ── line-crossing (entry    │
│                  camera only) → ENTRY/  │
│                  EXIT, visitor_id        │
│  reid.py   ── appearance Re-ID →        │
│                REENTRY de-dup           │
│  staff.py  ── uniform + behavioural     │
│                heuristic → is_staff     │
│  zones.py  ── point-in-polygon dwell    │
│                → ZONE_ENTER/EXIT/DWELL  │
│                  BILLING_QUEUE_JOIN     │
│  emit.py   ── Pydantic validation +     │
│                JSONL write              │
│  to_official ─ project canonical →      │
│                Purplle sample_events    │
│  run.py    ── orchestrates all cameras  │
└──────┬───────────────────────┬──────────┘
       │ events/events.jsonl    │ events/events.official.jsonl
       │ (canonical, seeds API) │ (matches sample_events schema)
       ▼                        ▼
 replay.py  (streams JSONL → API)   graders' deliverable
                 │
                 ▼
┌─────────────────────────────────────────┐
│  FastAPI service  (docker compose up)   │
│                                         │
│  POST /events/ingest                    │
│      │  idempotent (event_id PK)        │
│      ▼                                  │
│  SQLite  events  table                  │
│      │                                  │
│  GET /stores/{id}/metrics               │
│  GET /stores/{id}/funnel                │
│  GET /stores/{id}/heatmap               │
│  GET /stores/{id}/anomalies             │
│  GET /health                            │
│      │                                  │
│  POS CSV ─── conversion.py             │
│              (time-window + brand-dwell)│
└─────────────────────────────────────────┘
```

---

## Data flow in detail

### 1 — Detection (pipeline/)

Each video clip is processed independently. YOLOv8s detects persons per frame; ByteTrack assigns stable `track_id`s across frames (handles occlusion by keeping low-confidence boxes in the association graph rather than dropping them).

The **entrance camera** (`CAM 3 - entry.mp4`, `CAM_ENTRY`, `role: entry`) is the single source of truth for entry/exit counts. A virtual horizontal line segment (`entry_line` in `config/cameras.json`) separates the outside (street side, y < line_y) from the inside. A track whose centroid crosses inbound → `ENTRY`; outbound → `EXIT`. No other camera emits `ENTRY`/`EXIT`, which prevents double-counting across camera field-of-view overlaps (R9).

### 2 — Session model

Each inbound line-crossing opens a **visitor session**: a stable `visitor_id` token (UUID-based) associated with that `track_id` for the camera's lifetime. `session_seq` in event metadata records how many events belong to the same session.

On a new inbound crossing the Re-ID check (colour histogram cosine similarity + 5-minute time gate) runs first. A match against a recently exited visitor → `REENTRY`, reusing the prior `visitor_id`. This collapses re-entries throughout the API: `COUNT(DISTINCT visitor_id)` on ENTRY/REENTRY events gives the true unique-visitor count without double-counting.

### 3 — Zone tracking

The two zone cameras (`CAM_ZONE_1` = top-shelf bays, `CAM_ZONE_2` = bottom-shelf bays) and the billing camera (`CAM_BILLING`) track foot position (bbox bottom-centre) against zone polygons defined in `config/zones.json`. The entry camera also carries an `FOH` (front-of-house) zone. Ray-casting point-in-polygon fires:

- `ZONE_ENTER` on first crossing into a polygon
- `ZONE_DWELL` every 30 s of continuous presence (cumulative `dwell_ms`)
- `ZONE_EXIT` on leaving
- `BILLING_QUEUE_JOIN` on entering the `CASH_COUNTER` zone
- `BILLING_QUEUE_ABANDON` on leaving without a correlated POS transaction

### 4 — Staff exclusion

Two signals combined: (a) colour histogram similarity to a staff uniform palette and (b) behavioural indicators — ≥4 distinct zones visited, or ≥900-frame single-zone dwell, or ≥3 billing-zone entries per session. Events are emitted with `is_staff=true`; exclusion happens at query time (R4). The heuristic is configurable and documented as bounded in CHOICES.md.

### 5 — Ingestion (API)

`POST /events/ingest` accepts up to 500 events per call. The SQLite `events` table uses `event_id TEXT PRIMARY KEY` so `INSERT OR IGNORE` gives idempotency (R10). Malformed events return a per-item error in the response body (partial success, not a batch rollback).

### 6 — Analytics queries

All endpoints query live from SQLite — no materialised views, no precomputed answers (R24).

**Metrics**: unique visitors = `COUNT(DISTINCT visitor_id)` on ENTRY/REENTRY where `is_staff=0`; abandonment = BILLING_QUEUE_ABANDON / BILLING_QUEUE_JOIN; queue depth = `queue_depth` from the most recent JOIN metadata.

**Funnel**: Entry → Zone Visit → Billing Queue → Purchase, session as the unit. Re-entries collapse to the original visitor_id via `COUNT(DISTINCT visitor_id)` on sub-queries.

**Heatmap**: per-zone visit frequency normalised to 0–100; `data_confidence=false` when fewer than 20 total sessions in the window (R13).

**Conversion** (two-method, R16):
- *Time-window*: a session with billing-zone presence within 5 minutes before a POS `order_time` → converted (baseline, per brief).
- *Brand-dwell*: a session that dwelled in the brand bay matching a POS transaction's `brand_name` → additionally converted. Surfaced separately and combined; the brand method is the differentiator that maps "which zone drives which brand's sales."

**Anomalies**: queue spike (depth ≥ 5 → CRITICAL, ≥ 3 → WARN), dead zone (no zone entries in 30 min → INFO), conversion drop (recent 2-hour proxy vs day baseline → WARN). All include a `suggested_action` string for operational response.

**Health**: last event timestamp per store; `STALE_FEED=true` if >10 minutes behind replay clock (R15).

---

## Output schema: canonical vs official

The pipeline carries **two** event representations:

- **Canonical** (`app/schema.py`, `events/events.jsonl`) — the internal contract the analytics API, SQLite store, and 71 tests run on. Upper-case event types (`ENTRY`, `ZONE_ENTER`, `BILLING_QUEUE_JOIN`…), one flat shape with `metadata`.
- **Official** (`events/events.official.jsonl`) — the exact shape of the graders' `data/sample_eventsbe42122.jsonl`: lower-case `entry`/`zone_entered`/`queue_completed`, per-family field sets (`id_token`, `zone_name`/`zone_type`/`is_revenue_zone`, queue lifecycle, hotspots).

`pipeline/to_official.py` is a pure function from canonical → official, so the official file is fully reproducible without Docker/OpenCV (`python pipeline/to_official.py`). Keeping the canonical stream as source-of-truth means conforming to the contract never destabilises the tested API. Mapping:

| Canonical | Official | Notes |
|---|---|---|
| `ENTRY` / `EXIT` | `entry` / `exit` | `visitor_id` → stable `id_token` |
| `REENTRY` | `entry` | same `id_token` (a returning visitor) |
| `ZONE_ENTER` / `ZONE_EXIT` | `zone_entered` / `zone_exited` | zone metadata + centroid hotspot from `config/zones.json` |
| `ZONE_DWELL` | *(dropped)* | dwell is implied by the enter/exit pair |
| `BILLING_QUEUE_JOIN` + `…_ABANDON` | `queue_completed` / `queue_abandoned` | paired into one queue episode with `wait_seconds`, `queue_position_at_join` |

Fields the schema requires but in-store vision can't truthfully produce (`gender_pred`, `age_pred`, `age_bucket`, `group_id`, `group_size`) are emitted **null**, and `is_face_hidden` defaults **false** — present for conformance, never fabricated. Completed-vs-abandoned and `queue_served_ts` are heuristics over the observed billing episode; see CHOICES.md.

A conformance test (`tests/test_official_schema.py`) asserts the emitted keys match the sample file exactly, per event family.

---

## Storage choice

SQLite — zero extra container, clean `docker compose up`, ample for one store / one day of events. WAL mode for write concurrency. Postgres was considered and rejected; see CHOICES.md.

---

## AI-Assisted Decisions

Three places where LLM assistance shaped the design and what was accepted or overridden:

**1 — Dual conversion method (accepted)**
The plan initially described only the time-window method from the brief. When asked "how would a Purplle merchandising team actually want to know which zone drove a purchase?", the LLM surfaced the brand-dwell match: a customer dwelling in the DermDoc bay before a DermDoc transaction is a stronger attribution signal than time-window alone. This was adopted as the differentiating "brand-dwell" method that appears alongside the baseline. It directly maps to the POS `brand_name` column and required no extra infrastructure.

**2 — Staff behavioural heuristic threshold tuning (partially overridden)**
The LLM initially suggested a single composite score (colour + behaviour weighted sum). The implemented design uses separate thresholds with an OR/AND logic (`colour_match OR (multi_zone AND (long_dwell OR repeated_billing))`) because the colour signal alone is unreliable when staff aren't in uniform, and the weighted sum obscured which signal was firing. The separate-threshold design makes the boundary auditable — a key factor for a submission where honest limitations matter more than opaque precision.

**3 — Event schema: emit-time vs ingest-time validation (accepted)**
The LLM recommended running Pydantic validation at `emit.py` (pipeline-side) as well as at ingest. This was accepted: it catches bugs at the source (incorrect zone_id for ENTRY events, zero dwell_ms for ZONE_DWELL) rather than silently polluting the JSONL and discovering failures later at the API boundary. The shared `app/schema.py` module is imported by both pipeline and API, keeping the single source of truth.
