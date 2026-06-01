# Store Intelligence — Brigade Road, Bangalore

End-to-end CCTV → detection → event stream → analytics API for a single physical store (5 cameras).

## Instructions to Run

**Prerequisites:** Docker + Docker Compose.

### 1. Place the dataset

Put all **5 videos in `data/clips/`** with these exact names — they're looked up by name, not number.

> ⚠️ **The `CAM 1`–`CAM 5` numbering does NOT map 1:1 to the roles below.** Watch each clip first and match it to what it actually shows, then copy it to the matching target name. Don't assume `CAM 1` = entrance.

| Target name in `data/clips/` | What the clip should show |
|---|---|
| `entrance.mp4` | Entrance door — entry/exit counting line |
| `floor_top_brands.mp4` | Top-shelf brand bays |
| `floor_bottom_brands.mp4` | Bottom-shelf brand bays |
| `cash_counter.mp4` | Cash Counter / billing area |
| `accessories_area.mp4` | Accessories area / back corner |

Once you've identified which source file is which, copy and rename them (example — replace the source names with your actual mapping):

```bash
mkdir -p data/clips
cp "<clip showing entrance>"        data/clips/entrance.mp4
cp "<clip showing top brand bays>"  data/clips/floor_top_brands.mp4
cp "<clip showing bottom bays>"     data/clips/floor_bottom_brands.mp4
cp "<clip showing cash counter>"    data/clips/cash_counter.mp4
cp "<clip showing accessories>"     data/clips/accessories_area.mp4
```

The POS sales CSV can keep any name, anywhere under `data/` — auto-discovered.

### 2. Build & run

```bash
rm -f events/events.db                                  # delete any stale DB first (fresh seed)
docker compose build                                    # YOLOv8 baked in, runs offline
docker compose --profile pipeline run --rm pipeline     # 5 clips → events/events.jsonl
docker compose up -d api                                # API + Swagger docs on http://localhost:8000/docs
python3 replay.py --speed 0                             # seed the database
```

> **Note:** if `events/events.db` already exists, delete it before seeding (`rm -f events/events.db`). The DB is a generated runtime artifact; starting from a stale one can mix old and new events. A bundled `events/events.jsonl` is included so you can skip the detection pipeline and seed directly with `python3 replay.py --speed 0`.

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
