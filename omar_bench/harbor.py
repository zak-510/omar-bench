"""Harbor is the harness all three suites publish on. Build its command line; read back its trials."""
from __future__ import annotations

import json
import tomllib
from datetime import datetime
from pathlib import Path

from . import pricing, usage
from .config import ROOT

PYTHON = (ROOT / ".python-version").read_text().strip()


def command(c: dict, jobs_dir: Path, job_name: str, *, attempts: int | None = None,
            concurrency: int = 1, tasks: list[str] = (), limit: int | None = None,
            extra: list[str] = ()) -> list[str]:
    """The `harbor run` invocation for one cell, on the Harbor version the suite pins."""
    s, m = c["suite_cfg"], c["model_cfg"]
    extras = f"[{s['harbor_extras']}]" if s.get("harbor_extras") else ""
    version = f"=={s['harbor']}" if s.get("harbor") else ""  # empty only under --allow-unpinned
    # The Python the repo pins: left to itself uv takes the newest, where Harbor's
    # dependencies may not install yet (fastuuid had no 3.15 wheel on 2026-10-14).
    cmd = ["uvx", "--from", f"harbor{extras}{version}", "--python", PYTHON, "harbor", "run"]
    cmd += ["-p", s["path"]] if "path" in s else ["-d", s["dataset"]]
    cmd += ["-a", c["agent"], "-m", f"{m['provider']}/{m['id'] or '<unset>'}"]
    # Harbor's own retries are off: a retry is a new run with its own rows, never a replacement.
    cmd += ["-k", str(attempts or s["attempts"]), "-n", str(concurrency), "-r", "0"]
    # -y: Harbor otherwise stops to ask before some runs, and nobody is there to answer.
    cmd += ["-o", str(jobs_dir), "--job-name", job_name, "-y"]
    if s.get("env_file") and (ROOT / ".env").exists():
        cmd += ["--env-file", ".env"]
    for pattern in tasks or s.get("include_tasks", ()):
        cmd += ["-i", pattern]
    for name in s.get("exclude_tasks", ()):
        cmd += ["-x", name]
    if limit:
        cmd += ["-l", str(limit)]
    if s.get("allow_agent_host"):
        cmd += ["--allow-agent-host", m.get("api_host") or "<unset>"]
    # Suite settings first, so a model's own endpoint variables win.
    for key, value in {**s.get("agent_env", {}).get(c["backend"], {}), **m.get("env", {})}.items():
        cmd += ["--ae", f"{key}={value}"]

    if c["omar"] is None:
        kwargs = {"version": c["cli_version"]}
    else:
        a, o = c["arm_cfg"], c["omar"]
        # A fixed team, or the brief Mission Control's assistant designs one from.
        kwargs = ({"topology": str(ROOT / a["topology"])} if "topology" in a else
                  {"brief": str(ROOT / a["brief"]), "max_mutations": a.get("max_mutations")})
        kwargs |= {
            "backend": c["backend"],
            "cli_version": c["cli_version"],
            "omar_repo": o["repo"],
            "omar_version": o["version"],
            "omar_sha256": o["sha256_linux_amd64"],
            # OMAR's limit is per agent turn; Harbor's is on the attempt and still applies.
            "invocation_timeout_sec": s.get("agent_timeout_sec"),
        }
        if o.get("tarball"):  # a build of a commit, not a release
            kwargs |= {"omar_tarball": str(ROOT / o["tarball"]), "omar_spec": str(ROOT / o["spec"])}
    # The suite's own CLI settings (effort, disabled tools) go to every arm alike.
    kwargs |= s.get("agent_kwargs", {}).get(c["backend"], {})
    for key, value in kwargs.items():
        if value:
            cmd += ["--ak", f"{key}={value}"]
    return cmd + list(extra)


def _seconds(start: str | None, end: str | None) -> float | None:
    if not (start and end):
        return None
    return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds(), 3)


def _reward(result: dict) -> float | None:
    verifier = result.get("verifier_result") or {}
    rewards = verifier.get("rewards")
    if isinstance(rewards, dict) and rewards:
        return float(rewards["reward"] if "reward" in rewards else next(iter(rewards.values())))
    if isinstance(verifier.get("reward"), (int, float)):
        return float(verifier["reward"])
    return None


def _json(path: Path):
    """The object in a JSON file; None if the file is absent, False if it cannot be read."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return False
    return data if isinstance(data, dict) else False


def trials(run_dir: Path):
    """(trial directory, its config, its result) for every trial Harbor started under a run.

    Harbor writes a trial's config.json when it starts and its result.json when it
    ends, so a trial that was cut off has the first and not the second. `result` is
    None when the file is absent and False when it cannot be read.
    """
    found: list[Path] = []
    candidates = {p.parent for name in ("config.json", "result.json")
                  for p in Path(run_dir).rglob(name)}
    for trial_dir in sorted(candidates):
        if any(parent in trial_dir.parents for parent in found):
            continue  # a file of the same name inside a trial's own logs or artifacts
        cfg, result = _json(trial_dir / "config.json"), _json(trial_dir / "result.json")
        if result and not {"trial_name", "task_name"} <= result.keys():
            # Not a trial's result: beside a config it is a job's own summary, alone it is debris.
            result = None if cfg else False
        started = bool(cfg) and "trial_name" in cfg
        if started or result or (result is False and (trial_dir / "agent").is_dir()):
            found.append(trial_dir)
            yield trial_dir, cfg if started else None, result


def _task(cfg: dict | None, trial_dir: Path) -> str:
    """The task name Harbor would have put in the result, for a trial that has none."""
    task = (cfg or {}).get("task") or {}
    if task.get("name"):
        return task["name"]
    if task.get("path"):
        try:
            return tomllib.loads((ROOT / task["path"] / "task.toml").read_text())["task"]["name"]
        except (OSError, KeyError, ValueError):
            return Path(task["path"]).name
    return trial_dir.name.rsplit("__", 1)[0]


def _workspace_files(trial_dir: Path) -> int | None:
    """How many files an OMAR team left in its own workspaces instead of the task's directory."""
    listing = trial_dir / "agent" / "omar-workspace-files.txt"
    return len(listing.read_text().splitlines()) if listing.exists() else None


def rows(run_dir: Path, manifest: dict, prices: dict) -> list[dict]:
    """Ledger rows for every trial Harbor started under `run_dir`: errored and cut-off ones too."""
    out = []
    for trial_dir, cfg, result in trials(run_dir):
        by_model = usage.scan(trial_dir, pricing.long_over(prices))
        tokens = usage.total(by_model)
        usd, unpriced = pricing.cost(by_model, prices)
        cost_source = f"list-price@{prices.get('as_of')}" if usd is not None else None
        if result:
            name, task = result["trial_name"], result["task_name"]
            error_type = (result.get("exception_info") or {}).get("exception_type")
        else:
            # Started and never finished, or its result was cut off mid-write: still an attempt.
            result = {}
            name, task = (cfg or {}).get("trial_name") or trial_dir.name, _task(cfg, trial_dir)
            error_type = "NoResult" if not (trial_dir / "result.json").exists() else "UnreadableResult"
        if not by_model:
            # No session logs survived; fall back to what Harbor's agent reported, and say so.
            agent = result.get("agent_result") or {}
            usd, unpriced = agent.get("cost_usd"), []
            cost_source = "harbor" if usd is not None else None
        reward = _reward(result)
        out.append({
            "schema": 2,
            # One row per trial directory, whatever state it was found in.
            "attempt_id": f"{manifest['run_id']}/{name}",
            "harbor_trial_id": result.get("id"),
            "run_id": manifest["run_id"],
            "study": manifest["study"],
            "cell_sha256": manifest["cell_sha256"],
            "operator": manifest["operator"],
            "auth": manifest["auth"],
            "suite": manifest["suite"], "arm": manifest["arm"], "model": manifest["model"],
            "suite_label": manifest.get("suite_label"),
            "model_id": manifest["model_id"],
            "config_sha256": manifest["config_sha256"],
            "task": task,
            "task_checksum": result.get("task_checksum"),
            "trial": name,
            # pass/fail come from the suite's own verifier; "error" means it never produced a reward.
            "outcome": "error" if reward is None else ("pass" if reward >= 1.0 else "fail"),
            "reward": reward,
            "error_type": error_type,
            "started_at": result.get("started_at"),
            "wall_secs": _seconds(result.get("started_at"), result.get("finished_at")),
            "usage": by_model,
            "tokens": tokens,
            "served_models": usage.served_models(by_model),
            "cost_usd": usd,
            "cost_source": cost_source,
            "unpriced_models": unpriced,
            "workspace_files": _workspace_files(trial_dir),
            "trial_dir": str(trial_dir.relative_to(run_dir.parent)),
        })
    return out


def planned(run_dir: Path) -> dict[str, int] | None:
    """Attempts planned per task, from the lock file Harbor writes before it starts any."""
    counts: dict[str, int] | None = None
    for path in sorted(Path(run_dir).rglob("lock.json")):
        lock = _json(path)
        if lock and isinstance(lock.get("trials"), list):
            counts = counts or {}
            for entry in lock["trials"]:
                name = (entry.get("task") or {}).get("name") or "?"
                counts[name] = counts.get(name, 0) + 1
    return counts


def account(run_dir: Path, attempted: list[dict]) -> dict:
    """What a run set out to do against what it did. A trial that never started is not an attempt."""
    plan = planned(run_dir)
    total = sum(plan.values()) if plan is not None else None
    cut_off = sorted(row["attempt_id"] for row in attempted
                     if row["error_type"] in ("NoResult", "UnreadableResult"))
    unstarted = None if total is None else max(total - len(attempted), 0)
    return {"planned": total, "attempted": len(attempted), "without_result": cut_off,
            "unstarted": unstarted,
            # Only when the plan is known and every planned trial ran to a result.
            "complete": total is not None and not unstarted and not cut_off}
