"""Token usage from agent session logs.

Every arm is measured with this one instrument, whether one CLI ran or an OMAR
team of them, so costs are comparable across arms. Reads what the CLIs already
write: Claude Code transcripts and Codex rollouts.
"""
from __future__ import annotations

import json
from pathlib import Path

FIELDS = ("input", "cache_read", "cache_write_5m", "cache_write_1h", "output", "requests")
LONG = "@long"  # suffix for requests billed at a model's long-prompt rate


def _zero() -> dict:
    return dict.fromkeys(FIELDS, 0)


def _lines(path: Path):
    with open(path, errors="replace") as f:
        for line in f:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line from a killed session
            if isinstance(record, dict):
                yield record


def claude(paths, long_over: dict[str, int] | None = None) -> dict[str, dict]:
    """Usage per model from Claude Code transcripts.

    A transcript repeats one API response on several lines (one per content
    block), each carrying the full usage, so lines are keyed by message id and
    counted once. Summing lines overcounts several times over.
    """
    long_over = long_over or {}
    seen: dict[str, tuple[str, dict]] = {}
    for path in paths:
        for record in _lines(path):
            message = record.get("message")
            if record.get("type") != "assistant" or not isinstance(message, dict):
                continue
            usage, model = message.get("usage"), message.get("model")
            if not isinstance(usage, dict) or not model or model == "<synthetic>":
                continue
            key = message.get("id") or record.get("requestId") or record.get("uuid")
            seen[key] = (model, usage)

    out: dict[str, dict] = {}
    for model, usage in seen.values():
        written = usage.get("cache_creation_input_tokens") or 0
        by_ttl = usage.get("cache_creation") or {}
        w1h = by_ttl.get("ephemeral_1h_input_tokens") or 0
        w5m = by_ttl.get("ephemeral_5m_input_tokens") or (written - w1h)
        fresh, read = usage.get("input_tokens") or 0, usage.get("cache_read_input_tokens") or 0
        prompt = fresh + read + w5m + w1h
        name = model + LONG if prompt > long_over.get(model, float("inf")) else model
        row = out.setdefault(name, _zero())
        row["input"] += fresh
        row["cache_read"] += read
        row["cache_write_5m"] += w5m
        row["cache_write_1h"] += w1h
        row["output"] += usage.get("output_tokens") or 0
        row["requests"] += 1
    return out


def codex(paths, long_over: dict[str, int] | None = None) -> dict[str, dict]:
    """Usage per model from Codex rollouts (rollout-*.jsonl).

    Codex 0.159 writes a token_usage_record for each API response. Each is
    counted once, keyed by response id, so a rollout copied into a forked
    session does not count twice, and a response whose prompt is over the
    model's threshold is priced at its long rate. Older rollouts carry only a
    running total in token_count events; the last one is the session's usage,
    all priced short. Codex counts cached reads inside input_tokens, and
    reasoning inside output_tokens. Cache writes are taken to be inside
    input_tokens too; no rollout has shown one yet.
    """
    long_over = long_over or {}
    responses: dict[str, tuple[str, dict]] = {}
    totals: list[tuple[str, dict, int]] = []
    for path in paths:
        model, total, events, records = "unknown", None, 0, 0
        for record in _lines(path):
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            kind = record.get("type")
            if kind == "turn_context" and payload.get("model"):
                model = payload["model"]
            elif kind == "token_usage_record" and isinstance(payload.get("usage"), dict):
                responses[payload.get("response_id") or f"{path}:{records}"] = (model, payload["usage"])
                records += 1
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                if info.get("total_token_usage"):
                    total = info["total_token_usage"]
                    events += 1
        if not records and total is not None:
            totals.append((model, total, events))

    out: dict[str, dict] = {}

    def add(name: str, usage: dict, requests: int) -> None:
        cached = usage.get("cached_input_tokens") or 0
        written = usage.get("cache_write_input_tokens") or 0
        row = out.setdefault(name, _zero())
        row["input"] += (usage.get("input_tokens") or 0) - cached - written
        row["cache_read"] += cached
        row["cache_write_5m"] += written
        row["output"] += usage.get("output_tokens") or 0
        row["requests"] += requests

    for model, usage in responses.values():
        prompt = usage.get("input_tokens") or 0
        add(model + LONG if prompt > long_over.get(model, float("inf")) else model, usage, 1)
    for model, total, events in totals:
        add(model, total, events)
    return out


def scan(root: Path, long_over: dict[str, int] | None = None) -> dict[str, dict]:
    """Usage per model for every session log under `root`."""
    logs = sorted(Path(root).rglob("*.jsonl"))
    rollouts = [p for p in logs if p.name.startswith("rollout-")]
    transcripts = [p for p in logs if not p.name.startswith("rollout-")]
    out = claude(transcripts, long_over)
    for model, row in codex(rollouts, long_over).items():
        merged = out.setdefault(model, _zero())
        for field in FIELDS:
            merged[field] += row[field]
    return out


def total(by_model: dict[str, dict]) -> dict:
    out = _zero()
    for row in by_model.values():
        for field in FIELDS:
            out[field] += row[field]
    return out


def served_models(by_model: dict[str, dict]) -> list[str]:
    """Model ids the provider reported serving: the attestation a configured id is not."""
    return sorted({name.removesuffix(LONG) for name in by_model})
