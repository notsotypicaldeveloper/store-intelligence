# Store Intelligence

## Submission Deliverables

| File | Location | Notes |
|---|---|---|
| Event log (JSONL) | [`events.jsonl`](events.jsonl) | Official schema — follows `sample_events.jsonl`; 976 events from 4 cameras |
| README | [`README.md`](README.md) | This file |
| Design doc | [`docs/DESIGN.md`](docs/DESIGN.md) | Architecture, data flow, session model, AI-Assisted Decisions section |
| Choices doc | [`docs/CHOICES.md`](docs/CHOICES.md) | Model selection, schema design, API/storage decisions |

> To regenerate `events.jsonl` without Docker: `python3 pipeline/to_official.py` (reads `events/events.jsonl`, writes repo-root `events.jsonl`).

End-to-end CCTV → detection → event stream → analytics API for a single physical store (4 cameras).

## Instructions to Run

**Prerequisites:** Docker + Docker Compose.

### 1. Place the dataset

Put the **4 camera videos directly in `data/`** (no `clips/` subfolder) with their original names — they're looked up by name in `config/cameras.json`:

| File in `data/` | Role | Camera ID |
|---|---|---|
| `CAM 3 - entry.mp4` | Entrance door — entry/exit counting line | `CAM_ENTRY` |
| `CAM 1 - zone.mp4` | Top-shelf brand bays | `CAM_ZONE_1` |
| `CAM 2 - zone.mp4` | Bottom-shelf brand bays | `CAM_ZONE_2` |
| `CAM 5 - billing.mp4` | Cash Counter / billing queue | `CAM_BILLING` |

> ℹ️ The feeds are already named by role, so no renaming step is needed. There is **no CAM 4**, and `CAM 1`/`CAM 2` are *both* zone feeds. If a zone clip turns out to show the other set of bays, swap `CAM_ZONE_1`/`CAM_ZONE_2` in `config/cameras.json` (or just the `clip_filename`s). All feeds are 1920×1080.

The POS sales CSV (`data/pos_transactions.csv`) can keep any name, anywhere under `data/` — auto-discovered.

### 2. Build & run

```bash
rm -f events/events.db                                  # delete any stale DB first (fresh seed)
docker compose build                                    # YOLOv8 baked in, runs offline
docker compose --profile pipeline run --rm pipeline     # 4 feeds → events/events.jsonl (+ events.official.jsonl)
docker compose up -d api                                # API + Swagger docs on http://localhost:8000/docs
python3 replay.py --speed 0                             # seed the database
```

> **Note:** if `events/events.db` already exists, delete it before seeding (`rm -f events/events.db`). The DB is a generated runtime artifact; starting from a stale one can mix old and new events. A bundled `events/events.jsonl` is included so you can skip the detection pipeline and seed directly with `python3 replay.py --speed 0`.

> **Two output files — by design, not duplication.** The pipeline writes **`events/events.jsonl`** (the internal/canonical stream — the single source of truth the analytics API and tests run on) and **`events/events.official.jsonl`** (the graded deliverable, projected into the Purplle `sample_events` schema). They're kept separate on purpose: the official schema is a deliberately *lossy* contract (dwell events are folded into their zone enter/exit pair, each billing join+close becomes one `queue_*` record), so it can't drive the analytics — hence the official file has fewer rows than the canonical one. The official file is a pure, tested function of the canonical one (locked by `tests/test_official_schema.py`) and can be regenerated without Docker:
> ```bash
> python3 pipeline/to_official.py   # events/events.jsonl → events/events.official.jsonl
> ```
> Demographic fields (`gender_pred`/`age_pred`/`group_id`…) are emitted **null** — present for schema conformance, not fabricated. See [`docs/CHOICES.md`](docs/CHOICES.md).

### 3. Query analytics

Open **http://localhost:8000/docs**, or:

```bash
curl localhost:8000/health
curl localhost:8000/stores/ST1008/metrics   | python3 -m json.tool
curl localhost:8000/stores/ST1008/funnel    | python3 -m json.tool
curl localhost:8000/stores/ST1008/heatmap   | python3 -m json.tool
curl localhost:8000/stores/ST1008/anomalies | python3 -m json.tool
```

### 4. (Optional) Live dashboard

```bash
docker compose --profile dashboard up dashboard   # in Docker
# or locally (API must be up):
pip install rich && python3 dashboard.py --speed 50
```

> **Note:** app code is baked into the image. After changing code under `app/`, rebuild with `docker compose up -d --build api` — `docker compose restart` alone keeps the old code.

---

## How it works

```
4 camera feeds  →  YOLOv8s + ByteTrack  →  JSONL events  →  replay.py  →  FastAPI + SQLite
                                              │
                                              └─→  events.official.jsonl (sample_events schema)
```

1. **Detection pipeline** (`docker compose run pipeline`): processes each feed with YOLOv8s person detection and ByteTrack for stable track IDs. The entrance camera (`CAM 3 - entry.mp4`, `CAM_ENTRY`) detects line-crossings → ENTRY/EXIT events with unique `visitor_id` tokens. Re-entry de-dup runs a colour-histogram Re-ID against recently exited visitors. Staff are flagged via a uniform-colour + behavioural heuristic (multi-zone movement, long dwell, repeated cash-counter visits). Zone enter/exit/dwell events use point-in-polygon on the foot position. All events are validated against the Pydantic schema at emit time.

2. **Replay** (`python replay.py`): streams `events/events.jsonl` into `POST /events/ingest` in configurable batches. Re-running is idempotent — duplicate `event_id`s are ignored.

3. **API** (`docker compose up`): all analytics computed live from SQLite on every request. No hardcoded/precomputed answers.

---

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Service status + per-store last-event timestamp; STALE_FEED warning if >10 min lag |
| `POST /events/ingest` | Batch ingest (≤500 events), idempotent by `event_id`, partial success on malformed |
| `GET /stores/{id}/metrics` | Unique visitors, conversion rate, avg dwell/zone, queue depth, abandonment rate |
| `GET /stores/{id}/funnel` | Entry → Zone Visit → Billing Queue → Purchase; re-entries collapsed; staff excluded |
| `GET /stores/{id}/heatmap` | Zone visit frequency (0–100) + avg dwell; `data_confidence=false` when <20 sessions |
| `GET /stores/{id}/anomalies` | Queue spike, conversion drop, dead zone; severity INFO/WARN/CRITICAL + suggested action |

**Store ID for Brigade Road:** `ST1008`

---

## Camera calibration

Zone polygons and the entry line are configured in `config/cameras.json` and `config/zones.json`. To visually verify or adjust them against real frames:

```bash
# Inside Docker (has OpenCV + ffmpeg):
docker compose run --rm pipeline python pipeline/calibrate.py --extract
docker compose run --rm pipeline python pipeline/calibrate.py --overlay --camera CAM_ENTRY
docker compose run --rm pipeline python pipeline/calibrate.py --overlay --camera CAM_ZONE_1
# Overlay image written to data/_frames/<clip-stem>_overlay.jpg
```

---

## Running tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

71 tests covering: schema validation, idempotent ingest, session-based funnel, heatmap confidence, POS conversion (time-window + brand-dwell), anomaly detection (queue spike, dead zone), health stale-feed, pipeline line-crossing, Re-ID, staff heuristic, zone tracking, emit/schema round-trips, and official `sample_events` schema conformance.

---

## Docs

- [`docs/DESIGN.md`](docs/DESIGN.md) — Architecture, data flow, session model, AI-assisted decisions
- [`docs/CHOICES.md`](docs/CHOICES.md) — Three decisions: detection model, event schema, storage/runtime
