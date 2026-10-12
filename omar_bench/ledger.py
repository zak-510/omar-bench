"""The attempt ledger: one JSON line per attempt, appended, never rewritten.

Every attempt Harbor made is a row, including the ones that errored. What a
report counts is decided at report time (see exclusions.toml), not by deleting
rows, so an attempt-level failure rate always exists.

One file per run. Several people can add results to the same repository
without ever editing the same file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def read(directory: Path) -> list[dict]:
    rows = []
    for path in sorted(directory.glob("*.jsonl")):
        with open(path) as f:
            rows += [json.loads(line) for line in f if line.strip()]
    return rows


def append(directory: Path, run_id: str, rows: list[dict]) -> int:
    """Append to the run's own file the rows not already in the ledger. Returns how many."""
    have = {row["attempt_id"] for row in read(directory)}
    new = [row for row in rows if row["attempt_id"] not in have]
    if new:
        directory.mkdir(parents=True, exist_ok=True)
        with open(directory / f"{run_id}.jsonl", "a") as f:
            for row in new:
                f.write(json.dumps(row, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
    return len(new)


def record(directory: Path, run_id: str, data: dict) -> None:
    """A run's own line of account: what it planned against what it attempted. Written once."""
    path = directory / f"{run_id}.json"
    if not path.exists():
        directory.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def records(directory: Path) -> dict[str, dict]:
    return {path.stem: json.loads(path.read_text()) for path in sorted(directory.glob("*.json"))}
