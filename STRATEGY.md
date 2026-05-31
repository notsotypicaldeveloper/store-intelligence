---
name: Store Intelligence
last_updated: 2026-05-30
---

# Store Intelligence Strategy

## Target problem

Apex/Purplle's physical stores are a data blind spot — mature real-time analytics
online, near-zero visibility into in-store footfall, conversion, dwell, or queues. This
is a timed hiring challenge: the real problem is turning raw, messy CCTV from one real
store (Brigade Road, Bangalore — 5 cameras) into trustworthy live store analytics within
a 48-hour window, without the de-dup and staff-counting errors that inflate every
vendor's numbers.

## Our approach

Gate-first, then correctness-first, with every decision documented at the moment it's
made. Get a thin end-to-end slice past the acceptance gate early so we're always
submittable, then pour the remaining hours into the detection accuracy and API
correctness that a human reviewer can verify in a 10-minute eyeball. Use proven
off-the-shelf tools (not research). Because grading is manual and includes an integrity
check, every output must visibly vary with input (no hardcoding) and every choice must
be defensible in CHOICES.md.

## Who it's for

**Primary:** Two human reviewers (UpGrad/Purplle), each spending ~10 minutes: run
`docker compose up`, inspect generated events, validate `/metrics` + `/funnel` outputs,
read DESIGN.md/CHOICES.md, assign a score. They reward functional correctness,
engineering judgment, and clear reasoning over model complexity; they cap hardcoded or
input-invariant outputs at 50. Target: 85+ = strong, 70–85 = interview.

**Secondary:** The Brigade Road store/ops manager whose real needs (trustworthy
conversion, dwell-by-brand, queue alerts) keep our metric definitions sensible rather
than just demo-passing.

## Key metrics

- **Acceptance-gate green** — `docker compose up` runs with no manual steps, `/metrics`
  returns a valid response, the pipeline produces structured events, DESIGN.md +
  CHOICES.md exist and are non-trivial, system doesn't crash. Binary; must become true
  early and stay true. (Failing the gate = rejected before scoring.)
- **Output consistency & integrity** — `/metrics` values are logically consistent,
  `/funnel` shows sane drop-off, and outputs change when the input events change. Proxy
  for the 35-pt API slice and the integrity check (hardcoding caps score at 50).
- **Approximate entry-count match** — detection entry/exit counts are close to a manual
  count on a sample clip, with re-entry / staff / group cases visibly handled. Proxy for
  the 30-pt detection slice (no ground-truth file exists; reviewers eyeball a clip).
- **Parts-complete burn-down** — which of Detection / API / Production / Thinking are
  "done & defensible" against the 48h clock. Forces points-per-hour prioritization.

## Tracks

### Detection & Counting (30 pts)

Person detection, tracking, entry/exit direction at the bottom-left door, per-session
visitor tokens, and the hard edge cases: group entry, staff exclusion, re-entry de-dup,
occlusion, multi-camera overlap. Zones = the named brand bays + makeup/nail units + cash
counter, defined as polygons from the floor-plan. Emits a self-defined, schema-consistent
event stream (no sample_events.jsonl is provided — we define and document the schema).

_Why it serves the approach:_ Foundation everything depends on, and a third of the score;
approximate-but-honest counts beat brittle precision.

### Intelligence API & Anomalies (35 pts)

Idempotent ingest, real-time metrics, session-based funnel (re-entries not double-counted),
heatmap by brand zone, and anomaly detection with severity. Conversion correlates visitor
sessions to the real POS export — by billing-zone time window and, where possible, by
brand-bay dwell → matching brand purchase.

_Why it serves the approach:_ Largest single bucket; logically-consistent, input-varying
outputs are exactly what reviewers validate in their 3-minute API check.

### Production Readiness (20 pts)

Containerization (`docker compose up`, zero manual steps), structured logging /
observability, graceful degradation, and tests covering key + edge scenarios. Owns the
acceptance gate.

_Why it serves the approach:_ The gate is pass/fail for being scored at all; this keeps us
"always submittable" and seamless to run.

### Engineering Thinking & Decisions (15 pts)

DESIGN.md (clear architecture) and CHOICES.md (model choice, schema rationale, one API
decision — options considered, trade-offs, what we chose and why), written at decision
time. Demonstrates ownership, not generic filler.

_Why it serves the approach:_ Directly scored, the tie-breaker, and the artifact reviewers
read to judge whether we actually own our decisions.

## Milestones

- **2026-05-30** — 48-hour clock started (dataset downloaded). Strategy reconciled to
  the real dataset.
- **Gate green** — thin end-to-end slice passes the 5-point acceptance gate.
- **Detection honest** — entry/exit counts approx-match a manual count on a clip;
  re-entry / staff / group visibly handled; schema consistent.
- **API correct** — all endpoints return logically consistent, input-varying outputs;
  funnel is session-based; conversion uses the real POS data.
- **Production hardened** — docker, logging, tests done on a clean machine; README ≤5
  commands.
- **Docs done** — DESIGN.md + CHOICES.md non-trivial and honest.
- **Submission** — repo invited, `docker compose up` verified clean on a fresh checkout.

## Not working on

- Face recognition / biometric identity (faces blurred; privacy) — Re-ID leans on
  clothing/trajectory, not faces.
- Training or fine-tuning any model — off-the-shelf only.
- Multi-store scale, auth, or multi-tenancy — dataset is one store, 5 cameras.
- Chasing precise detection metrics — reviewers want approximate-but-honest counts and
  good edge-case handling, not benchmark accuracy.
- Part E live dashboard (bonus, not in the PDF rubric) — stretch only, after the scored
  parts are solid.
