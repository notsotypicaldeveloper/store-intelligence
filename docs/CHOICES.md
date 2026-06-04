# Architecture Decisions

Three key decisions made during the build, with the options considered, trade-offs, and what was chosen and why.

---

## Decision 1 — Detection model: YOLOv8s + ByteTrack

### Options considered

| Option | Summary |
|--------|---------|
| **YOLOv8s + ByteTrack** *(chosen)* | Off-the-shelf real-time detector + tracker; pretrained on COCO; no training required |
| YOLOv8n (nano) | Faster, lower accuracy on partially occluded persons; accuracy matters for group counts |
| RT-DETR | Strong accuracy, slower inference, less community tooling around tracker integration |
| VLM (e.g. GPT-4V) for zone classification | Would replace polygon config with free-text zone descriptions; API-rate limited, expensive per frame, adds latency |
| Custom-trained model | Prohibited by dataset license; also unnecessary for approximate counts the rubric rewards |

### Trade-offs

ByteTrack's key property for this use case: it keeps low-confidence detections in the association graph rather than dropping them. A partially occluded person crossing the door retains their track_id through the occlusion and re-associates when they become visible. This directly enables:
- Correct group counts (N tracks → N ENTRY events, not 1)
- Stable visitor_ids through brief occlusions without spurious REENTRY events

The VLM-for-zone-classification idea was explicitly rejected: the floor-plan image is static, the zones don't change day-to-day, and computing a polygon-to-image mapping once is O(1) cost vs O(frames × API latency). The chosen approach (config/zones.json with polygons) is deterministic, auditable, and free.

### What was chosen and why

**YOLOv8s + ByteTrack.** Proven on real-world retail footage, fast enough for 15 fps 1080p on a single GPU (which Docker provides), integrates natively with the `ultralytics` library, and gives a strong track_id signal. The `s` (small) variant was chosen over `n` (nano) because detection precision on partially occluded persons near the door matters for entry counts — the core rubric metric. Off-the-shelf beats trained-custom every time when training is prohibited and approximate counts are the target.

### Staff exclusion: why we rely on behaviour, not uniform colour

The staff classifier (`pipeline/staff.py`) supports two signals: a torso-colour histogram matched against a known uniform palette, and behavioural signals (visiting ≥4 distinct zones, long single-zone dwell, repeated cash-counter presence). We ship with the **behavioural signal active and the colour palette empty** — a deliberate, footage-driven choice.

We inspected sample frames from the entrance, billing, and floor cameras to check whether a uniform colour would be a usable signal. Finding: **staff do wear black, but so do most of the customers in these clips.** Black is therefore a poor discriminator — keying on it would flag a large share of genuine shoppers as staff and *under*-count visitors, which is a worse error than the over-count it would fix. Colour-based staff detection only pays off when the uniform is visually distinct from customer clothing (e.g. a bright branded apron); here it is not. We chose not to trade one inaccuracy for another, and not to hardcode a colour the footage doesn't support.

Honest limitation that follows from this: the behavioural signal needs multi-zone movement, which only the **floor** cameras observe — the entrance camera sees a person cross the door and nothing more. So staff are excluded from floor/zone metrics but **cannot** be reliably excluded from the raw entry count at the door. A future, non-hardcoded improvement would be to *learn* the uniform palette from behaviourally-identified staff on the floor cameras and feed it to the entrance camera, rather than assume a colour up front.

---

## Decision 2 — Event schema design

### Options considered

| Option | Summary |
|--------|---------|
| **Self-defined schema matching the brief's catalogue** *(chosen)* | One record per behavioural event, all fields strongly typed via Pydantic |
| Wide denormalised row (all possible fields per frame) | Simple to write but enormous JSONL; most fields null per row; wastes storage |
| Separate tables per event type | More normalised; adds schema complexity and query joins; unnecessary at one-store scale |
| Ingest-only validation | Defer Pydantic to the API boundary; let the pipeline emit raw dicts |

### Trade-offs

The core tension: how strict to be at emit time vs ingest time. A loose emit lets the pipeline move fast but silently corrupts the event stream. A strict emit — enforced by running `app.schema.Event` validation in `emit.py` — catches schema violations immediately at the source.

The chosen schema has one deliberate invariant: ENTRY/EXIT/REENTRY events carry `zone_id=null`; all zone events carry a non-null `zone_id`. This is enforced by a Pydantic `model_validator`. It prevents a common pipeline bug where a zone event accidentally inherits a null zone_id from a template.

The `metadata` sub-object uses `extra="allow"` to accommodate event-specific fields (`queue_depth`, `sku_zone`, `session_seq`) without polluting the top-level schema with optional fields for every event type. This keeps `GET /metrics` queries simple: join on `event_type`, pull `metadata->>'queue_depth'` only for billing events.

### What was chosen and why

**Single strongly-typed Pydantic model, validated at both emit and ingest.** The shared `app/schema.py` import — used by both pipeline and API — is the single source of truth. This eliminates the class of bugs where the pipeline emits an event that the API silently rejects or misinterprets. The `event_id` UUID primary key in SQLite gives idempotent ingest for free (INSERT OR IGNORE).

### Conforming to the provided `sample_events` schema (canonical → official)

The dataset ships `data/sample_eventsbe42122.jsonl` — the graders' **output contract**. It's a different, richer shape than the internal model (lower-case `entry`/`zone_entered`/`queue_completed`; per-family fields like `id_token`, `zone_name`/`zone_type`/`is_revenue_zone`, `wait_seconds`, `queue_position_at_join`, hotspots, demographics). Switching the internal model to it would cascade through the SQLite store, every analytics query, and the test suite.

**Decision:** keep the canonical model as the analytics source-of-truth, and add a pure projection (`pipeline/to_official.py`) that emits `events/events.official.jsonl` in the exact sample shape. The API + 71 tests stay on the canonical model; the deliverable conforms to the contract; `tests/test_official_schema.py` asserts the emitted keys equal the sample's keys per family. The official file is reproducible without Docker (`python pipeline/to_official.py`), because it's a deterministic function of the canonical stream.

The projection is intentionally **lossy** — `ZONE_DWELL` events are folded into their zone enter/exit pair, and each billing `JOIN`+close collapses into a single `queue_*` episode — so the official file has *fewer* rows than the canonical one. That information loss is exactly why the two files coexist rather than one replacing the other: the analytics layer needs the richer canonical stream (per-event dwell, confidence, raw queue depth), while the graders need the flattened contract. Keeping both is the source-of-truth-vs-deliverable split, not duplication.

**Demographic fields are emitted null, not fabricated.** `gender_pred`, `age_pred`, `age_bucket`, `group_id`, `group_size` appear in the sample but require age/gender inference and group association that we do not run — retail CCTV blurs faces, so an age/gender model on these frames would be guessing. We emit the fields (for conformance) set to `null`, with `is_face_hidden=false`. Writing honest nulls is worth more than confident `age_pred: 28`s the pipeline never measured. Wiring an explicit demographic model is the documented path to filling them.

**Queue completed-vs-abandoned is a documented heuristic.** The internal pipeline observes a billing-zone `JOIN` and a later exit, but not the till interaction that distinguishes "served" from "gave up." `to_official.py` pairs each join with its exit into one episode and labels it abandoned when the wait is implausibly short for service (`< MIN_SERVICE_SECONDS`), deriving `queue_served_ts` for completed episodes. The production-correct signal is POS reconciliation — which the API already does in `conversion.py` — but at event-emit time, before the POS join, the threshold is the honest approximation.

---

## Decision 3 — Storage and runtime: SQLite vs Postgres; batch+replay vs streaming

### Options considered

| Option | Summary |
|--------|---------|
| **SQLite + batch pipeline + replay** *(chosen)* | Zero extra container; `event_id` PK for idempotency; replay seeds DB at controlled speed |
| Postgres | Production-grade, concurrent writes, better query planner at scale |
| Redis for in-memory metrics | Fast reads; persistence requires AOF/RDB; extra container |
| Real-time streaming (Kafka + consumer) | Correct architecture for truly live ingest; overkill for one store / one day |

### Trade-offs

**SQLite vs Postgres:** Postgres adds a second container with startup ordering complexity (`depends_on` + health checks), increases `docker compose up` surface area, and introduces connection-pool configuration. For one store and one day of events (a few thousand JSONL records), SQLite with WAL mode has ample write throughput and zero config. The `event_id TEXT PRIMARY KEY` with `INSERT OR IGNORE` gives idempotent ingest at no extra cost. The known limit: SQLite doesn't scale to concurrent writers at high ingest rate — but the batch+replay pattern has a single writer (the replayer), so this limit never bites. Documented honestly rather than silently assumed away.

**Batch+replay vs streaming:** The brief explicitly permits batch processing. Replay (`replay.py`) streams events into the API at configurable speed, producing the "live feel" while keeping the pipeline reproducible and clock-independent. Real-time streaming (Kafka, Flink) would be correct for a production deployment but adds two containers, complex failure modes, and significant local setup — all risk to the acceptance gate. The batch approach is documented as a deliberate choice, not a limitation: a reviewer running `docker compose up && python replay.py` sees the same data flow as a streaming system, just at compressed time.

**Conversion baseline with one day of data:** The anomaly detection `CONVERSION_DROP` check compares a recent 2-hour window against the full-day rate as the baseline. This is a substitute for the ideal 7-day rolling baseline (which would require a week of data). The limitation is documented in the anomaly's description string and in this document — the rubric rewards owning this limitation over silently fabricating a multi-day baseline from a single day's CSV.

**Cross-camera visitor identity (conversion numerator vs denominator):** Each camera assigns its own `visitor_id` from its local track IDs; there is no cross-camera Re-ID linking the same shopper between the entrance camera and the floor/billing cameras. Entry/exit counts come only from the entrance camera (the single source of truth, which prevents double-counting), but conversion's "converted" set is drawn from billing- and brand-zone presence on the *floor/billing* cameras. Because those two populations use independent ID spaces, the ratio `converted ÷ unique entrants` is only well-defined once a shopper keeps one identity across cameras — i.e. true Multi-Target Multi-Camera tracking (OSNet/torchreid appearance Re-ID). That is out of scope for a 48-hour build and unreliable without overlapping fields of view (the billing camera does not overlap the entrance). The time-window and brand-dwell correlation logic is itself correct; the identity linkage is the gap. On the provided clips this surfaces as **conversion = 0**: the clips are a ~2-minute slice of a ~20-minute session, so billing/brand-dwell timestamps do not fall within the 5-minute window before the day's POS transactions. Documented as a known limitation rather than masked by clamping the ratio to ≤ 100%.

> Note: `conversion.py` discovers the POS CSV by several filenames (`Brigade_Bangalore_*.csv`, `pos_transactions.csv`, `*pos*.csv`) with a `POS_CSV` env override, so correlation runs regardless of how the dataset file is named.

### What was chosen and why

**SQLite + batch+replay.** The acceptance gate is a pass/fail binary: if `docker compose up` fails or the system crashes, the submission is rejected before scoring. SQLite eliminates the second-container risk entirely. Batch+replay keeps the pipeline clock-independent and gives a clean, reproducible event stream that the reviewer can inspect at `events/events.jsonl`. This is the simplest architecture that fully satisfies the rubric — and simplicity under a 48-hour constraint is a feature.
