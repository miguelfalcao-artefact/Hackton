#!/usr/bin/env python3
"""Collect unseen JSONL log records into one cumulative CSV file."""

from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path


FIELDS = [
    "record_id", "ts", "level", "service", "logger", "event", "message",
    "request_id", "method", "path", "route", "status", "latency_ms",
    "api_key_id", "tier", "error_code", "raw_json",
]


def record_id(record: dict) -> str:
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def read_known_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    with path.open(newline="", encoding="utf-8") as stream:
        return {row["record_id"] for row in csv.DictReader(stream) if row.get("record_id")}


def collect(source: Path, destination: Path, known_ids: set[str]) -> tuple[int, list[str]]:
    rows: list[dict[str, object]] = []
    warnings: list[str] = []
    with source.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"line {number}: malformed JSON")
                continue
            if not isinstance(record, dict):
                warnings.append(f"line {number}: record is not an object")
                continue
            identifier = record_id(record)
            if identifier in known_ids:
                continue
            rows.append({
                "record_id": identifier,
                "ts": record.get("ts", ""),
                "level": record.get("level", ""),
                "service": record.get("service", ""),
                "logger": record.get("logger", ""),
                "event": record.get("event", ""),
                "message": record.get("msg", record.get("message", "")),
                "request_id": record.get("request_id", ""),
                "method": record.get("method", ""),
                "path": record.get("path", ""),
                "route": record.get("route", ""),
                "status": record.get("status", ""),
                "latency_ms": record.get("latency_ms", ""),
                "api_key_id": record.get("api_key_id", ""),
                "tier": record.get("tier", ""),
                "error_code": record.get("error_code", ""),
                # Keeps every source field, including fields not promoted to columns.
                "raw_json": json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            })
            known_ids.add(identifier)

    if rows:
        destination.parent.mkdir(parents=True, exist_ok=True)
        new_file = not destination.exists() or destination.stat().st_size == 0
        with destination.open("a", newline="", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            if new_file:
                writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
            fcntl.flock(stream, fcntl.LOCK_UN)
    return len(rows), warnings


def log_family(path: Path) -> list[Path]:
    """Return rotated siblings plus the active log file."""
    rotated = sorted(
        (candidate for candidate in path.parent.glob(f"{path.name}.*") if candidate.is_file()),
        key=lambda candidate: candidate.stat().st_mtime_ns,
    )
    return [*rotated, path]


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Collect new JSONL logs into a cumulative CSV every 30 seconds")
    parser.add_argument("--api-log", type=Path, action="append", help="JSONL source; repeat for multiple sources (rotated siblings are included)")
    parser.add_argument("--csv", type=Path, default=project / "runtime/logs.csv")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 0.2:
        parser.error("interval must be at least 0.2 seconds")
    configured = args.api_log or [project / "error_generator/logs/api.jsonl"]
    sources = [source.resolve(strict=True) for source in configured]
    if any(not source.is_file() for source in sources):
        parser.error("each API log must be a file")
    input_files = list(dict.fromkeys(file for source in sources for file in log_family(source)))
    destination = args.csv.resolve()
    known_ids = read_known_ids(destination)
    print(json.dumps({"status": "MONITORING", "sources": [str(path) for path in input_files], "interval_seconds": args.interval}), flush=True)

    while True:
        try:
            added = 0
            warnings: list[str] = []
            # Rediscover rotations on every pass so rotations created at runtime are included.
            input_files = list(dict.fromkeys(file for source in sources for file in log_family(source)))
            for source in input_files:
                source_added, source_warnings = collect(source, destination, known_ids)
                added += source_added
                warnings.extend(f"{source.name}: {warning}" for warning in source_warnings)
            print(json.dumps({"status": "UPDATED" if added else "UNCHANGED", "added": added, "total": len(known_ids), "csv": str(destination), "sources": [str(path) for path in input_files], "warnings": warnings}), flush=True)
        except OSError as exc:
            print(json.dumps({"status": "ERROR", "error": type(exc).__name__, "message": str(exc)}), flush=True)
            if args.once:
                return 1
        if args.once:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
