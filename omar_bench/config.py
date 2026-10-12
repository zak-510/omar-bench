"""bench.toml: load it, resolve one (suite, arm, model) cell, name what is still unpinned."""
from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Harbor's built-in agent for each CLI backend.
HARBOR_AGENT = {"claude": "claude-code", "codex": "codex"}
OMAR_AGENT = "omar_bench.agents.omar_agent:OmarAgent"


class ConfigError(Exception):
    pass


def load(path: Path | None = None) -> dict:
    path = path or ROOT / "bench.toml"
    raw = path.read_bytes()
    cfg = tomllib.loads(raw.decode())
    cfg["_sha256"] = hashlib.sha256(raw).hexdigest()
    cfg["_raw"] = raw  # a run saves exactly the bytes it was configured from
    return cfg


def _get(cfg: dict, table: str, name: str) -> dict:
    try:
        return cfg[table][name]
    except KeyError:
        known = ", ".join(sorted(cfg.get(table, {})))
        raise ConfigError(f"unknown {table[:-1]} {name!r} (have: {known})") from None


def cell(cfg: dict, suite: str, arm: str, model: str) -> dict:
    """One runnable configuration, with every pin it depends on gathered in one place."""
    s, a, m = _get(cfg, "suites", suite), _get(cfg, "arms", arm), _get(cfg, "models", model)
    if a["kind"] == "todo":
        raise ConfigError(f"arm {arm!r} is not implemented: {a.get('note', '')}")
    backend = m["backend"]
    out = {
        "suite": suite, "arm": arm, "model": model,
        "suite_cfg": s, "arm_cfg": a, "model_cfg": m,
        "backend": backend,
        "cli_version": cfg["cli"].get(backend, ""),
        "agent": HARBOR_AGENT[backend] if a["kind"] == "harbor" else OMAR_AGENT,
        "omar": cfg["omar"][a["omar"]] if a["kind"] == "omar" else None,
    }
    return out


def identity(c: dict, **also) -> str:
    """Hash of everything that must match for two runs' attempts to be one result.

    The cell's own pins, not the whole file: pinning another model must not split this one.
    """
    doc = {k: c[k] for k in ("suite", "arm", "model", "suite_cfg", "arm_cfg", "model_cfg",
                             "omar", "cli_version")} | also
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def unpinned(c: dict) -> list[str]:
    """Names of the values this cell needs that are still empty."""
    s, m = c["suite_cfg"], c["model_cfg"]
    need = {
        f"suites.{c['suite']}.harbor": s.get("harbor"),
        f"models.{c['model']}.id": m.get("id"),
        f"cli.{c['backend']}": c["cli_version"],
    }
    if "path" in s:
        need[f"suites.{c['suite']}.ref"] = s.get("ref")
    else:
        need[f"suites.{c['suite']}.dataset"] = s.get("dataset")
    for key, value in m.get("env", {}).items():
        need[f"models.{c['model']}.env.{key}"] = value
    if s.get("allow_agent_host"):
        need[f"models.{c['model']}.api_host"] = m.get("api_host")
    if c["omar"] is not None:
        # Without it OMAR would stop an agent at its own default, sooner than the suite does.
        need[f"suites.{c['suite']}.agent_timeout_sec"] = s.get("agent_timeout_sec")
        which = c["arm_cfg"]["omar"]
        need[f"omar.{which}.version"] = c["omar"].get("version")
        need[f"omar.{which}.sha256_linux_amd64"] = c["omar"].get("sha256_linux_amd64")
    return sorted(k for k, v in need.items() if not v)
