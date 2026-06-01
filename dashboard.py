#!/usr/bin/env python3
"""
Live terminal dashboard: replays events/events.jsonl → POST /events/ingest
and polls /stores/{id}/metrics so store KPIs update live on screen.

This proves the pipeline and API are genuinely connected: each ingestion batch
triggers a real API call; metrics reflect exactly the events ingested so far.

Usage:
    python dashboard.py [--file PATH] [--api URL] [--speed FACTOR] [--store-id ID]

Examples:
    python dashboard.py --speed 50          # 50× faster than real time
    python dashboard.py --speed 0           # flood as fast as possible
    python dashboard.py --api http://api:8000   # inside docker network
"""
import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime


# ── rich imports ─────────────────────────────────────────────────────────────

try:
    from rich.columns import Columns
    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import BarColumn, MofNCompleteColumn, Progress, SpinnerColumn, TextColumn
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("ERROR: 'rich' is not installed. Run: pip install rich", file=sys.stderr)
    sys.exit(1)


# ── shared state (written by ingestion thread, read by render loop) ──────────

@dataclass
class LiveState:
    total: int = 0
    ingested: int = 0
    accepted: int = 0
    rejected: int = 0
    rate_eps: float = 0.0          # events / second
    last_event_type: str = "—"
    last_zone: str = "—"
    last_camera: str = "—"
    metrics: dict = field(default_factory=dict)
    funnel: list = field(default_factory=list)
    recent_events: deque = field(default_factory=lambda: deque(maxlen=8))
    error: str = ""
    done: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


# ── HTTP helpers (stdlib only) ────────────────────────────────────────────────

def _post(url: str, body: bytes, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(url: str, timeout: int = 5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


# ── ingestion worker thread ───────────────────────────────────────────────────

def _ingest_worker(
    lines: list[str],
    api: str,
    speed: float,
    store_id: str,
    batch_size: int,
    state: LiveState,
) -> None:
    """
    Groups events into 1-second simulated-time windows, posts each window as a
    single /events/ingest call, then polls /metrics.  Runs in a daemon thread.
    """
    ingest_url = f"{api}/events/ingest"
    metrics_url = f"{api}/stores/{store_id}/metrics"
    funnel_url = f"{api}/stores/{store_id}/funnel"

    # Parse timestamps so we can bucket by simulated time
    parsed: list[tuple[float, str]] = []
    for line in lines:
        try:
            ev = json.loads(line)
            # Convert ISO timestamp → unix epoch for arithmetic
            ts_str = ev.get("timestamp", "")
            try:
                dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                epoch = dt.timestamp()
            except Exception:
                epoch = 0.0
            parsed.append((epoch, line))
        except Exception:
            pass

    if not parsed:
        state.error = "events.jsonl is empty or unreadable"
        state.done = True
        return

    parsed.sort(key=lambda x: x[0])
    sim_start = parsed[0][0]
    wall_start = time.monotonic()

    bucket: list[str] = []
    last_sim_second = 0.0
    rate_window: deque[tuple[float, int]] = deque(maxlen=20)  # (wall_time, count)

    for epoch, line in parsed:
        if state.done:
            break

        sim_elapsed = epoch - sim_start

        # Sleep to maintain requested playback speed
        if speed > 0:
            wall_elapsed = time.monotonic() - wall_start
            target_wall = sim_elapsed / speed
            sleep_dur = target_wall - wall_elapsed
            if sleep_dur > 0:
                time.sleep(sleep_dur)

        bucket.append(line)

        # Flush a batch when we've crossed a 1-simulated-second boundary
        # or accumulated batch_size events
        if sim_elapsed - last_sim_second >= 1.0 or len(bucket) >= batch_size:
            events_to_post = [json.loads(l) for l in bucket]
            bucket = []
            last_sim_second = sim_elapsed

            try:
                body = json.dumps({"events": events_to_post}).encode()
                result = _post(ingest_url, body)

                now = time.monotonic()
                rate_window.append((now, len(events_to_post)))
                eps = _compute_eps(rate_window)

                # Grab last event for display
                last_ev = events_to_post[-1]
                last_type = last_ev.get("event_type", "—")
                last_zone = last_ev.get("zone_id") or "—"
                last_cam = last_ev.get("camera_id", "—")

                with state.lock:
                    state.ingested += len(events_to_post)
                    state.accepted += result.get("accepted", 0)
                    state.rejected += result.get("rejected", 0)
                    state.rate_eps = eps
                    state.last_event_type = last_type
                    state.last_zone = last_zone
                    state.last_camera = last_cam
                    for ev in events_to_post[-3:]:
                        state.recent_events.append(ev)

                # Poll metrics (non-blocking: skip on error)
                try:
                    m = _get(metrics_url)
                    with state.lock:
                        state.metrics = m
                except Exception:
                    pass

                # Poll funnel every ~5 seconds of wall time
                if int(now) % 5 == 0:
                    try:
                        f = _get(funnel_url)
                        with state.lock:
                            state.funnel = f.get("stages", [])
                    except Exception:
                        pass

            except urllib.error.URLError as exc:
                with state.lock:
                    state.error = f"API unreachable: {exc}"
                break

    # Post any remaining events in the final bucket
    if bucket:
        try:
            events_to_post = [json.loads(l) for l in bucket]
            body = json.dumps({"events": events_to_post}).encode()
            result = _post(ingest_url, body)
            with state.lock:
                state.ingested += len(events_to_post)
                state.accepted += result.get("accepted", 0)
                state.rejected += result.get("rejected", 0)
        except Exception:
            pass

    # Final metrics snapshot
    try:
        m = _get(metrics_url)
        f = _get(funnel_url)
        with state.lock:
            state.metrics = m
            state.funnel = f.get("stages", [])
    except Exception:
        pass

    with state.lock:
        state.done = True


def _compute_eps(window: deque) -> float:
    if len(window) < 2:
        return 0.0
    total_events = sum(c for _, c in window)
    duration = window[-1][0] - window[0][0]
    return round(total_events / duration, 1) if duration > 0 else 0.0


# ── rich rendering ─────────────────────────────────────────────────────────────

SEVERITY_COLOR = {"CRITICAL": "bold red", "WARN": "yellow", "INFO": "cyan"}
EVENT_COLOR = {
    "ENTRY": "green", "EXIT": "dim", "REENTRY": "green",
    "ZONE_ENTER": "cyan", "ZONE_EXIT": "dim cyan",
    "ZONE_DWELL": "bright_cyan",
    "BILLING_QUEUE_JOIN": "yellow", "BILLING_QUEUE_ABANDON": "red",
}


def _header(state: LiveState) -> Panel:
    store_id = state.metrics.get("store_id", "ST1008")
    status = "[blink bold green]● LIVE[/]" if not state.done else "[bold yellow]✓ COMPLETE[/]"
    title = Text.assemble(
        ("  Store Intelligence  ", "bold white on dark_blue"),
        "  ",
        (store_id, "bold cyan"),
        "  ",
        status,
    )
    if state.error:
        title = Text(f"  ⚠  {state.error}  ", style="bold red on dark_red")
    return Panel(title, style="bold dark_blue", padding=(0, 1))


def _metrics_table(state: LiveState) -> Table:
    m = state.metrics
    t = Table.grid(padding=(0, 2))
    t.add_column(style="dim", min_width=20)
    t.add_column(style="bold white", min_width=12)

    visitors = m.get("unique_visitors", 0)
    conv = m.get("conversion_rate", 0.0)
    queue = m.get("queue_depth", 0)
    abandon = m.get("abandonment_rate", 0.0)

    t.add_row("Unique Visitors", f"[bold green]{visitors}[/]")
    t.add_row("Conversion Rate", f"[bold {'green' if conv > 0.15 else 'yellow'}]{conv:.1%}[/]")
    t.add_row("Queue Depth", f"[bold {'red' if queue > 5 else 'yellow' if queue > 2 else 'green'}]{queue}[/]")
    t.add_row("Abandonment Rate", f"[bold {'red' if abandon > 0.3 else 'yellow'}]{abandon:.1%}[/]")
    return t


def _zone_table(state: LiveState) -> Table:
    dwell = state.metrics.get("avg_dwell_per_zone", {})
    t = Table("Zone", "Avg Dwell", style="dim", box=None, padding=(0, 1))
    t.columns[0].style = "cyan"
    t.columns[1].style = "bold white"
    if not dwell:
        t.add_row("—", "no data yet")
        return t
    for zone_id, ms in sorted(dwell.items(), key=lambda x: -x[1])[:8]:
        secs = ms / 1000
        t.add_row(zone_id, f"{secs:.0f}s")
    return t


def _funnel_table(state: LiveState) -> Table:
    t = Table("Stage", "Count", "Drop-off", box=None, padding=(0, 1))
    t.columns[0].style = "white"
    t.columns[1].style = "bold green"
    t.columns[2].style = "yellow"
    for stage in state.funnel:
        t.add_row(
            stage.get("stage", ""),
            str(stage.get("count", 0)),
            f"{stage.get('drop_off_pct', 0.0):.1%}",
        )
    if not state.funnel:
        t.add_row("—", "—", "awaiting data")
    return t


def _recent_events_table(state: LiveState) -> Table:
    t = Table("Time", "Camera", "Type", "Zone/Visitor", box=None, padding=(0, 1))
    t.columns[0].style = "dim"
    for ev in reversed(list(state.recent_events)):
        etype = ev.get("event_type", "")
        color = EVENT_COLOR.get(etype, "white")
        zone = ev.get("zone_id") or ev.get("visitor_id", "")[:12]
        ts = ev.get("timestamp", "")[-8:].replace("Z", "")  # HH:MM:SS
        t.add_row(ts, ev.get("camera_id", ""), f"[{color}]{etype}[/]", zone)
    if not state.recent_events:
        t.add_row("—", "—", "—", "waiting for events…")
    return t


def _pipeline_bar(state: LiveState, progress: Progress, task_id) -> None:
    progress.update(task_id, completed=state.ingested)


def _build_layout(state: LiveState, progress: Progress, task_id) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="pipeline", size=5),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )
    layout["body"]["left"].split_column(
        Layout(name="metrics", ratio=1),
        Layout(name="funnel", ratio=1),
    )
    layout["body"]["right"].split_column(
        Layout(name="zones", ratio=1),
        Layout(name="feed", ratio=1),
    )

    with state.lock:
        layout["header"].update(_header(state))

        layout["metrics"].update(Panel(
            _metrics_table(state),
            title="[bold]Live Metrics[/]",
            border_style="green",
        ))
        layout["funnel"].update(Panel(
            _funnel_table(state),
            title="[bold]Customer Funnel[/]",
            border_style="blue",
        ))
        layout["zones"].update(Panel(
            _zone_table(state),
            title="[bold]Zone Dwell Time[/]",
            border_style="cyan",
        ))
        layout["feed"].update(Panel(
            _recent_events_table(state),
            title="[bold]Event Feed[/]",
            border_style="magenta",
        ))

        _pipeline_bar(state, progress, task_id)
        eps_text = f"  {state.rate_eps:.1f} ev/s   accepted={state.accepted}  rejected={state.rejected}"
        layout["pipeline"].update(Panel(
            progress,
            title=f"[bold]Pipeline[/] [dim]{eps_text}[/]",
            border_style="dark_orange",
        ))

    return layout


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Store Intelligence live terminal dashboard")
    p.add_argument("--file", default="events/events.jsonl", help="events JSONL file")
    p.add_argument("--api", default="http://localhost:8000", help="API base URL")
    p.add_argument("--speed", type=float, default=50.0,
                   help="replay speed multiplier (50=50× realtime, 0=max speed)")
    p.add_argument("--store-id", default="ST1008")
    p.add_argument("--batch-size", type=int, default=50,
                   help="max events per ingest call")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    console = Console()

    try:
        with open(args.file) as f:
            lines = [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        console.print(f"[red]ERROR:[/] {args.file} not found — run the pipeline first.")
        return 1

    total = len(lines)
    console.print(f"[bold cyan]Store Intelligence Dashboard[/] — {total} events → {args.api}")
    console.print(f"Speed: {args.speed}× realtime  |  batch-size: {args.batch_size}")
    console.print("[dim]Press Ctrl+C to exit[/]\n")
    time.sleep(1)

    state = LiveState(total=total)

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}"),
        BarColumn(bar_width=40),
        MofNCompleteColumn(),
        TextColumn("[dim]{task.percentage:.1f}%"),
        console=console,
    )
    task_id = progress.add_task("Ingesting events", total=total)

    # Start ingestion in background
    worker = threading.Thread(
        target=_ingest_worker,
        args=(lines, args.api, args.speed, args.store_id, args.batch_size, state),
        daemon=True,
    )
    worker.start()

    try:
        with Live(
            _build_layout(state, progress, task_id),
            console=console,
            refresh_per_second=4,
            screen=True,
        ) as live:
            while True:
                live.update(_build_layout(state, progress, task_id))
                with state.lock:
                    done = state.done
                if done:
                    time.sleep(0.5)  # one final render
                    live.update(_build_layout(state, progress, task_id))
                    break
                time.sleep(0.25)
    except KeyboardInterrupt:
        pass

    # Final summary outside the live panel
    with state.lock:
        m = state.metrics
    console.print("\n[bold green]── Final Snapshot ─────────────────────────────────[/]")
    console.print(f"  Unique visitors  : [bold]{m.get('unique_visitors', 0)}[/]")
    console.print(f"  Conversion rate  : [bold]{m.get('conversion_rate', 0.0):.1%}[/]")
    console.print(f"  Queue depth      : [bold]{m.get('queue_depth', 0)}[/]")
    console.print(f"  Abandonment rate : [bold]{m.get('abandonment_rate', 0.0):.1%}[/]")
    console.print(f"  Events accepted  : [bold]{state.accepted}[/]  rejected={state.rejected}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
