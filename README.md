# Store Intelligence — Brigade Road, Bangalore

End-to-end CCTV → detection → event stream → analytics API for a single physical store (5 cameras).

## Quick start (5 commands)

```bash
# 1. Clone and enter the repo
git clone <repo-url> store-intelligence && cd store-intelligence

# 2. Drop the challenge dataset into data/clips/ — rename clips to:
#    entrance.mp4, floor_top_brands.mp4, floor_bottom_brands.mp4,
#    cash_counter.mp4, accessories_area.mp4
#    Also: data/_layout_extracted.png
#          data/Brigade_Bangalore_10_April_26*.csv

# 3. Run the detection pipeline (outputs events/events.jsonl)
docker compose --profile pipeline run --rm pipeline

# 4. Start the API
docker compose up

# 5. Replay events into the API (seeds the database for live analytics)
python replay.py
```

The API is then available at **http://localhost:8000**
OpenAPI docs: http://localhost:8000/docs

### Part E — Live Dashboard

```bash
# Option A: in Docker (after step 3 above — events.jsonl must exist)
docker compose --profile dashboard up dashboard

# Option B: locally (API must be running on :8000)
pip install rich
python dashboard.py --speed 50        # 50× realtime, ~30s run
python dashboard.py --speed 0         # flood as fast as possible
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
