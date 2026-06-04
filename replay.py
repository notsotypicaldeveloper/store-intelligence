#!/usr/bin/env python3
"""
Stream events/events.jsonl → POST /events/ingest to seed the API database.

Usage:
    python replay.py [--file PATH] [--api URL] [--batch-size N] [--speed FACTOR]
"""
import argparse
import importlib.util
import json
import pathlib
import sys
import time
import urllib.request
import urllib.error


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay event JSONL into the Store Intelligence API")
    p.add_argument("--file", default="events/events.jsonl", help="path to events.jsonl")
    p.add_argument("--api", default="http://localhost:8000", help="API base URL")
    p.add_argument("--batch-size", type=int, default=100, help="events per ingest call")
    p.add_argument("--speed", type=float, default=0.0,
                   help="replay at N× realtime (0 = no delay, as fast as possible)")
    return p.parse_args()


def post_batch(api_url: str, events: list[dict]) -> dict:
    body = json.dumps({"events": events}).encode()
    req = urllib.request.Request(
        f"{api_url}/events/ingest",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def main() -> int:
    args = parse_args()
    try:
        with open(args.file) as f:
            lines = [l.strip() for l in f if l.strip()]
    except FileNotFoundError:
        print(f"ERROR: {args.file} not found. Run the pipeline first.", file=sys.stderr)
        return 1

    total = len(lines)
    print(f"Replaying {total} events → {args.api} (batch_size={args.batch_size})")

    accepted = rejected = 0
    for i in range(0, total, args.batch_size):
        batch = [json.loads(l) for l in lines[i:i + args.batch_size]]
        try:
            result = post_batch(args.api, batch)
            accepted += result.get("accepted", 0)
            rejected += result.get("rejected", 0)
            pct = min(100, round((i + len(batch)) / total * 100))
            print(f"  {pct:3d}% ({i + len(batch)}/{total}) accepted={accepted} rejected={rejected}")
        except urllib.error.URLError as exc:
            print(f"ERROR posting batch {i}: {exc}", file=sys.stderr)
            return 1

        if args.speed > 0 and i + args.batch_size < total:
            time.sleep(1.0 / args.speed)

    print(f"\nDone. accepted={accepted} rejected={rejected}")

    # Keep events.official.jsonl in sync with the source events.jsonl.
    try:
        from pipeline.to_official import convert_file
    except ModuleNotFoundError:
        spec = importlib.util.spec_from_file_location(
            "to_official",
            pathlib.Path(__file__).parent / "pipeline" / "to_official.py",
        )
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        convert_file = mod.convert_file  # type: ignore[attr-defined]

    official_path = pathlib.Path(args.file).parent / "events.official.jsonl"
    n = convert_file(args.file, official_path, "config/zones.json", "ST1008")
    print(f"events.official.jsonl updated ({n} events)")

    return 0 if rejected == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
