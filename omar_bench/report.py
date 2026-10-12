"""Ledger to the launch table: success rate, cost per verified success, Pareto frontier.

What may be pooled is decided here, strictly: one study, one configuration, one price
table per row; one task set and attempt count per comparison.
"""
from __future__ import annotations

import random
import statistics
import tomllib
from collections import defaultdict
from pathlib import Path


def exclusions(path: Path) -> dict[str, str]:
    """attempt_id -> reason. The only way an attempt leaves a denominator."""
    if not path.exists():
        return {}
    entries = tomllib.loads(path.read_text()).get("exclude", [])
    return {e["attempt_id"]: e["reason"] for e in entries}


def _bootstrap_ci(per_task: list[float], resamples: int = 2000) -> tuple[float, float]:
    """95% interval for the mean, resampling tasks: attempts on one task are not independent."""
    if len(per_task) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(0)
    means = sorted(statistics.fmean(rng.choices(per_task, k=len(per_task)))
                   for _ in range(resamples))
    return (means[int(0.025 * resamples)], means[int(0.975 * resamples) - 1])


class ReportError(Exception):
    pass


def _one(attempts: list[dict], field: str, what: str) -> None:
    """Refuse to pool attempts that were not run the same way."""
    seen: dict[str, str] = {}
    for a in attempts:
        if a.get(field) is not None:
            seen.setdefault(a[field], a["run_id"])
    if len(seen) > 1:
        a = attempts[0]
        runs = "; ".join(f"{value[:12]} in {run}" for value, run in sorted(seen.items()))
        raise ReportError(
            f"{a['study']} / {a['suite']} / {a['arm']} / {a['model']}: attempts made under "
            f"different {what} cannot be one result ({runs}). Give the newer runs their own "
            "--study, or take one side out in results/exclusions.toml with the reason.")


def summarize(rows: list[dict], excluded: dict[str, str],
              incomplete: frozenset[str] | set[str] = frozenset()) -> list[dict]:
    """One line per (study, suite, arm, model). An errored attempt is a failure unless excluded.

    Studies are never pooled: a pilot and the evaluation it led to are different results.
    `incomplete` names the runs that did not finish what they planned.
    """
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for row in rows:
        cells[(row["study"], row["suite"], row["arm"], row["model"])].append(row)

    out = []
    for (study, suite, arm, model), attempts in sorted(cells.items()):
        counted = sorted((a for a in attempts if a["attempt_id"] not in excluded),
                         key=lambda a: a.get("started_at") or "")
        _one(counted, "cell_sha256", "pins or settings")
        _one(counted, "cost_source", "price tables")
        by_task: dict[str, list[bool]] = defaultdict(list)
        for a in counted:
            by_task[a["task"]].append(a["outcome"] == "pass")
        if not by_task:
            continue
        k = min(len(v) for v in by_task.values())
        per_task = [statistics.fmean(v) for v in by_task.values()]
        passes = sum(sum(v) for v in by_task.values())
        costs = [a["cost_usd"] for a in counted]
        priced = all(c is not None for c in costs)
        spend = sum(costs) if priced else None
        walls = [a["wall_secs"] for a in counted if a["wall_secs"] is not None]
        low, high = _bootstrap_ci(per_task)
        out.append({
            "study": study, "suite": suite, "arm": arm, "model": model,
            "label": counted[0].get("suite_label") or suite,
            "task_names": frozenset(by_task),
            "tasks": len(by_task), "attempts": len(counted),
            # Attempts that raised, or that the verifier never scored.
            "errors": sum(a["outcome"] == "error" or bool(a.get("error_type")) for a in counted),
            "excluded": len(attempts) - len(counted),
            "k": k,
            "incomplete": any(a["run_id"] in incomplete for a in counted),
            # Mean over tasks of each task's pass fraction, so every task weighs the same.
            "pass_rate": statistics.fmean(per_task),
            "ci_low": low, "ci_high": high,
            # Reliability: the share of tasks solved on every one of the first k attempts.
            "pass_all_k": statistics.fmean(all(v[:k]) for v in by_task.values()),
            "pass_any_k": statistics.fmean(any(v[:k]) for v in by_task.values()),
            "cost_per_attempt": spend / len(counted) if priced else None,
            # Everything spent, failures included, divided by what the verifier accepted.
            "cost_per_success": spend / passes if priced and passes else None,
            "output_tokens_per_attempt": statistics.fmean(a["tokens"]["output"] for a in counted),
            "median_wall_secs": statistics.median(walls) if walls else None,
            "comparable": True, "pareto": False,
        })

    for section in {(line["study"], line["suite"]) for line in out}:
        lines = [l for l in out if (l["study"], l["suite"]) == section]
        # Rows are comparable only on the same tasks with the same number of attempts.
        if len({(l["task_names"], l["k"]) for l in lines}) > 1:
            for l in lines:
                l["comparable"] = False
            continue
        points = [l for l in lines if l["cost_per_attempt"] is not None and not l["incomplete"]]
        for p in points:
            p["pareto"] = not any(
                q["pass_rate"] >= p["pass_rate"] and q["cost_per_attempt"] <= p["cost_per_attempt"]
                and (q["pass_rate"] > p["pass_rate"] or q["cost_per_attempt"] < p["cost_per_attempt"])
                for q in points)
    return out


def _usd(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def markdown(lines: list[dict]) -> str:
    out = []
    for study, suite in sorted({(l["study"], l["suite"]) for l in lines}):
        section = [l for l in lines if (l["study"], l["suite"]) == (study, suite)]
        out += [f"## {study}: {section[0]['label']}", "",
                "| arm | model | tasks | k | attempts | errors | excluded | pass rate (95% CI) "
                "| all-k | any-k | $/attempt | $/success | median s | Pareto |",
                "|---|---|--:|--:|--:|--:|--:|---|--:|--:|--:|--:|--:|:-:|"]
        for l in section:
            wall = "n/a" if l["median_wall_secs"] is None else f"{l['median_wall_secs']:.0f}"
            # No interval from a single task.
            ci = f" ({l['ci_low']:.1%} to {l['ci_high']:.1%})" if l["tasks"] > 1 else ""
            out.append(
                f"| {l['arm']}{' †' if l['incomplete'] else ''} | {l['model']} | {l['tasks']} "
                f"| {l['k']} | {l['attempts']} | {l['errors']} "
                f"| {l['excluded']} | {l['pass_rate']:.1%}{ci} "
                f"| {l['pass_all_k']:.1%} | {l['pass_any_k']:.1%} | {_usd(l['cost_per_attempt'])} "
                f"| {_usd(l['cost_per_success'])} | {wall} | {'yes' if l['pareto'] else ''} |")
        out.append("")
        if any(l["incomplete"] for l in section):
            out += ["† includes a run that did not finish what it planned, or whose plan is not "
                    "on record (results/runs/). Not a final result.", ""]
        if not section[0]["comparable"]:
            out += ["These rows cover different tasks or attempt counts, so their pass rates are "
                    "not comparable and no frontier is drawn.", ""]
    out += ["k is the fewest attempts any task has in that row; all-k and any-k use each task's "
            "first k.", "Errors are attempts that raised or were never scored. One the verifier still scored "
            "counts by that score; the rest count as failures. Cost is measured tokens at pinned "
            "list prices; n/a means an attempt in that row has no priced usage on record."]
    return "\n".join(out) + "\n"
