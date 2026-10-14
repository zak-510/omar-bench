#!/usr/bin/env python3
"""Recount the ledger from raw run directories, without the harness's own code.

For every attempt it recounts tokens from the agents' transcripts (Claude Code)
and rollouts (Codex, from their per-response records), reprices them
at the prices saved with the run, rereads the score, the exception and the
times from Harbor's files, and compares each with the ledger row. Where Claude
Code printed its own end-of-run totals (the `solo` arm), those are compared too.

    python3 tools/recount.py RUNS_DIR [RUN_ID ...]

RUNS_DIR holds one directory per run, as `runs/` does here and in the bucket.
Exits 1 if anything differs.
"""
import json
import sys
import tomllib
from datetime import datetime
from pathlib import Path

LEDGER = Path(__file__).resolve().parent.parent / "results" / "attempts"


def records(path):
    with open(path, errors="replace") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def seconds(start, end):
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def responses(transcripts):
    """One usage record per API response: a transcript repeats a response on several lines."""
    best = {}
    for path in transcripts:
        for record in records(path):
            message = record.get("message")
            if record.get("type") != "assistant" or not isinstance(message, dict):
                continue
            usage, model = message.get("usage"), message.get("model")
            if not isinstance(usage, dict) or not model or model == "<synthetic>":
                continue
            key = message.get("id")
            if key not in best or (usage.get("output_tokens") or 0) >= (best[key][1].get("output_tokens") or 0):
                best[key] = (model, usage)
    return best


def codex_responses(rollouts):
    """One usage record per Codex API response, in the shape a Claude transcript uses.

    Codex counts cached reads and cache writes inside input_tokens; a forked
    session repeats its parent's records, so they are keyed by response id.
    """
    best = {}
    for path in rollouts:
        model = None
        for record in records(path):
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if record.get("type") == "turn_context" and payload.get("model"):
                model = payload["model"]
            usage = payload.get("usage")
            if record.get("type") != "token_usage_record" or not isinstance(usage, dict) or not model:
                continue
            cached = usage.get("cached_input_tokens") or 0
            written = usage.get("cache_write_input_tokens") or 0
            best[payload.get("response_id")] = (model, {
                "input_tokens": (usage.get("input_tokens") or 0) - cached - written,
                "cache_read_input_tokens": cached, "cache_creation_input_tokens": written,
                "cache_creation": {"ephemeral_5m_input_tokens": written},
                "output_tokens": usage.get("output_tokens") or 0})
    return best


def total(best, prices):
    tokens = dict(input=0, cache_read=0, cache_write_5m=0, cache_write_1h=0, output=0, requests=0)
    usd, models = 0.0, set()
    for model, usage in best.values():
        models.add(model)
        price = prices["models"].get(model)
        written = usage.get("cache_creation_input_tokens") or 0
        by_ttl = usage.get("cache_creation") or {}
        w1h = by_ttl.get("ephemeral_1h_input_tokens") or 0
        w5m = by_ttl.get("ephemeral_5m_input_tokens") or (written - w1h)
        counts = dict(input=usage.get("input_tokens") or 0, cache_read=usage.get("cache_read_input_tokens") or 0,
                      cache_write_5m=w5m, cache_write_1h=w1h, output=usage.get("output_tokens") or 0)
        for name, count in counts.items():
            tokens[name] += count
        tokens["requests"] += 1
        if price is None:
            usd = None
        elif usd is not None:
            prompt = sum(counts.values()) - counts["output"]
            rate = price["long"] if "long_over" in price and prompt > price["long_over"] else price
            if any(count and name not in rate for name, count in counts.items()):
                usd = None  # tokens of a kind this model has no price for
            else:
                usd += sum(count * rate[name] for name, count in counts.items() if count) / 1e6
    return tokens, usd, sorted(models)


def check(run_dir):
    """(attempt id, {check: passed}) for every scored or raised attempt of one run."""
    prices = tomllib.loads((run_dir / "bench.toml").read_text())["prices"]
    rows = {row["trial"]: row for row in records(LEDGER / f"{run_dir.name}.jsonl")}
    for result_path in sorted(run_dir.glob("jobs/*/*/result.json")):
        trial, result = result_path.parent, json.loads(result_path.read_text())
        row = rows.get(result.get("trial_name"))
        if row is None:
            yield f"{run_dir.name}/{trial.name}", {"has a ledger row": False}
            continue
        rollouts = sorted(trial.glob("agent/sessions/**/rollout-*.jsonl"))
        best = responses(sorted(trial.glob("agent/sessions/projects/**/*.jsonl")))
        best.update(codex_responses(rollouts))
        tokens, usd, models = total(best, prices)
        reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        checks = {
            "tokens": all(tokens[name] == row["tokens"][name] for name in tokens),
            "cost": (usd is None and row["cost_usd"] is None) or tokens["requests"] == 0
                    or (usd is not None and abs(usd - (row["cost_usd"] or 0)) < 5e-7),
            "score": row["reward"] == reward,
            "outcome": row["outcome"] == ("error" if reward is None else "pass" if reward >= 1 else "fail"),
            "exception": row["error_type"] == (result.get("exception_info") or {}).get("exception_type"),
            "time": abs(row["wall_secs"] - seconds(result["started_at"], result["finished_at"])) < 0.001,
            "model served": row["served_models"] == models,
            "task": row["task"] == result["task_name"] and row["task_checksum"] == result.get("task_checksum"),
        }
        reward_file = trial / "verifier" / "reward.txt"
        if reward_file.exists() and reward is not None:
            checks["tests' own score"] = float(reward_file.read_text().strip()) == reward
        stream = trial / "agent" / "claude-code.txt"
        final = [r for r in records(stream) if r.get("type") == "result"] if stream.exists() else []
        for model, own in ((final[-1].get("modelUsage") or {}) if final else {}).items():
            checks[f"Claude Code's own totals ({model})"] = (
                own.get("inputTokens"), own.get("outputTokens"), own.get("cacheReadInputTokens"),
                own.get("cacheCreationInputTokens")) == (
                tokens["input"], tokens["output"], tokens["cache_read"],
                tokens["cache_write_5m"] + tokens["cache_write_1h"])
        yield row["attempt_id"], checks


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    root = Path(argv[1])
    runs = [root / name for name in argv[2:]] or sorted(p for p in root.iterdir() if (p / "manifest.json").exists())
    wrong = 0
    for run_dir in runs:
        for attempt, checks in check(run_dir):
            failed = [name for name, ok in checks.items() if not ok]
            wrong += len(failed)
            print(f"{'ok      ' if not failed else 'MISMATCH'} {attempt}  ({len(checks)} checks)"
                  + (f"  differs: {', '.join(failed)}" if failed else ""))
    print(f"{wrong} mismatches" if wrong else "all match")
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
