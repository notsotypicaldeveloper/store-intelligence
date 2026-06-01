# Store Intelligence — Brigade Road, Bangalore

End-to-end CCTV → detection → event stream → analytics API for a single physical store (5 cameras).

## Quick start

```bash
# 1. Clone and enter the repo
git clone <repo-url> store-intelligence && cd store-intelligence

# 2. Place the dataset in data/ — clips in data/clips/ named:
#    entrance.mp4, floor_top_brands.mp4, floor_bottom_brands.mp4,
#    cash_counter.mp4, accessories_area.mp4
#    POS CSV anywhere in data/ (e.g. pos_transactions.csv) — discovered automatically

# 3. Build images (YOLO weights baked in for offline runs)
docker compose build api pipeline dashboard

# 4. Detection pipeline → events/events.jsonl  (CPU-bound; logs progress per clip)
docker compose --profile pipeline run --rm pipeline    # skip if events.jsonl already exists

# 5. Start the API at http://localhost:8000
docker compose up -d api

# 6. Seed the database (use python3 if python is unaliased)
python3 replay.py --speed 0
```

The API is then available at **http://localhost:8000** — OpenAPI docs: http://localhost:8000/docs

```bash
# Query the analytics (or browse the docs UI above)
curl localhost:8000/health
curl localhost:8000/stores/ST1008/metrics   | python3 -m json.tool
curl localhost:8000/stores/ST1008/funnel    | python3 -m json.tool
curl localhost:8000/stores/ST1008/heatmap   | python3 -m json.tool
curl localhost:8000/stores/ST1008/anomalies | python3 -m json.tool
```

> **Note:** app code is baked into the image. After changing code under `app/`, rebuild with `docker compose up -d --build api` — `docker compose restart` alone keeps the old code.

### Part E — Live Dashboard

```bash
# Option A: in Docker (requires events.jsonl from the pipeline step)
docker compose --profile dashboard up dashboard

# Option B: locally (API must be running on :8000)
pip install rich
python3 dashboard.py --speed 50        # 50× realtime, ~30s run
python3 dashboard.py --speed 0         # flood as fast as possible
```

The dashboard streams `events/events.jsonl` into the API one simulated-second window at a time,
then polls `/stores/ST1008/metrics` after every batch — so the four KPI panels (visitors,
conversion rate, zone dwell, funnel) update live as events are ingested. Press `Ctrl-C` to exit.

---

## How it works

```
5 camera clips  →  YOLOv8s + ByteTrack  →  JSONL events  →  replay.py  →  FastAPI + SQLite
```

1. **Detection pipeline** (`docker compose run pipeline`): processes each clip with YOLOv8s person detection and ByteTrack for stable track IDs. The entrance camera (`entrance.mp4`, `CAM_ENTRY`) detects line-crossings → ENTRY/EXIT events with unique `visitor_id` tokens. Re-entry de-dup runs a colour-histogram Re-ID against recently exited visitors. Staff are flagged via a uniform-colour + behavioural heuristic (multi-zone movement, long dwell, repeated cash-counter visits). Zone enter/exit/dwell events use point-in-polygon on the foot position. All events are validated against the Pydantic schema at emit time.

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
# Overlay image written to data/_frames/entrance_overlay.jpg
```

---

## Running tests

```bash
pip install -r requirements.txt
pytest tests/ -v
```

63 tests covering: schema validation, idempotent ingest, session-based funnel, heatmap confidence, POS conversion (time-window + brand-dwell), anomaly detection (queue spike, dead zone), health stale-feed, pipeline line-crossing, Re-ID, staff heuristic, zone tracking, and emit/schema round-trips.

---

## Docs

- [`docs/DESIGN.md`](docs/DESIGN.md) — Architecture, data flow, session model, AI-assisted decisions
- [`docs/CHOICES.md`](docs/CHOICES.md) — Three decisions: detection model, event schema, storage/runtime
- [`STRATEGY.md`](STRATEGY.md) — Product strategy and approach
