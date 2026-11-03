"""Polly, Omnigent's coding orchestrator, as a benchmark treatment.

Polly is Omnigent's own: examples/polly at the pinned tag, kept as shipped in
omar_bench/agents/polly. `render()` changes it only where one model doing the whole task
requires, as on every other arm: its lead and its worker run the arm's model at the
suite's effort, the workers for CLIs that are not installed are left out, and a guardrail
refuses a dispatch that names another model (Polly picks models per dispatch, for cost
and for a second opinion).
"""
from __future__ import annotations

import json
from pathlib import Path

POLLY = Path(__file__).resolve().parent / "agents" / "polly"
WORKER = {"codex": "codex", "claude": "claude_code"}   # Polly's worker on each CLI
LEAD = {"codex": "codex", "claude": "claude-sdk"}      # the harness its lead runs on
# The lead's harness cannot take every effort its worker can: Omnigent's codex SDK wire
# stops at xhigh (omnigent/harness_capabilities.py at 0.20.0).
LEAD_EFFORT_CAP = {"codex": ("xhigh", {"max", "ultra"})}

# Polly as shipped, where it is changed.
LEAD_AS_SHIPPED = """executor:
  type: omnigent
  context_window: 1000000
  config:
    harness: claude-sdk
    # A pinned brain also pins the family its workers are routed within, which
    # strands the codex / pi sub-agents. Route the brain instead.
    smart_routing_harness: auto
"""
ROSTER_AS_SHIPPED = ("  agents:\n    - claude_code\n    - codex\n    - opencode\n    - cursor\n"
                     "    - hermes\n    - agy\n    - pi\n")
LAST_POLICY = "          allowed_purposes: [implement, review, explore, search]\n"
WORKER_EXECUTOR = "executor:\n  type: omnigent\n"


def _edit(text: str, old: str, new: str, where: str) -> str:
    if text.count(old) != 1:
        raise ValueError(f"Polly's {where} is not as shipped at the pinned tag; check {POLLY}")
    return text.replace(old, new)


def _pin(model: str, effort: str | None) -> str:
    return f"  model: {model}\n" + (f"  reasoning_effort: {effort}\n" if effort else "")


def guard(model: str) -> str:
    """A guardrail, in Polly's own policy list, refusing a dispatch that names another model.

    Omnigent's CEL policy abstains on anything but a map with a result, so the expression
    returns the refusal or an empty map.
    """
    m = json.dumps(model)
    named = (f"(has(event.data.arguments.args) && has(event.data.arguments.args.model)"
             f" && event.data.arguments.args.model != {m})"
             f" || (has(event.data.arguments.model) && event.data.arguments.model != {m})")
    reason = f"Every agent here runs {model}: send without a model, or with {model}."
    expression = ('event.type == "tool_call"'
                  ' && event.data.name in ["sys_session_send", "sys_session_create"]'
                  f" && ({named})"
                  f' ? {{"result": "DENY", "reason": {json.dumps(reason)}}} : {{}}')
    return ("    # omar-bench: one model does the whole task, as on every other arm.\n"
            "    single_model:\n"
            "      type: function\n"
            "      function:\n"
            "        path: omnigent.policies.builtins.cel.cel_policy\n"
            "        arguments:\n"
            f"          expression: {json.dumps(expression)}\n")


def render(backend: str, model: str, effort: str | None) -> dict[str, str]:
    """Polly's bundle with one model doing the whole task: path in the bundle -> text."""
    worker = WORKER[backend]
    cap, above = LEAD_EFFORT_CAP.get(backend, (None, set()))
    lead_effort = cap if effort in above else effort
    config = (POLLY / "config.yaml").read_text()
    config = _edit(config, LEAD_AS_SHIPPED,
                   "executor:\n  type: omnigent\n  context_window: 1000000\n"
                   + _pin(model, lead_effort) + f"  config:\n    harness: {LEAD[backend]}\n", "lead")
    config = _edit(config, ROSTER_AS_SHIPPED, f"  agents:\n    - {worker}\n", "roster")
    config = _edit(config, LAST_POLICY, LAST_POLICY + guard(model), "guardrails")
    files = {"config.yaml": config,
             f"agents/{worker}/config.yaml": _edit(
                 (POLLY / "agents" / worker / "config.yaml").read_text(),
                 WORKER_EXECUTOR, WORKER_EXECUTOR + _pin(model, effort), "worker")}
    for path in sorted((POLLY / "skills").rglob("*")):
        if path.is_file():
            files[str(path.relative_to(POLLY))] = path.read_text()
    return files
