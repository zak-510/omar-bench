"""OMAR programs as benchmark treatments.

Contract between a topology and the harness: `main` instantiates one team as
`t`; it takes `t.statement` (path to the task statement) and `t.cwd` (the
task's working directory) and writes `t.answer`, the team's final response to
the task. Suites that grade a written answer (APEX-Agents) grade that. (The
port was `t.task` until OMAR 0.4.3 made `task` a keyword.)
"""
from __future__ import annotations

import re

BACKENDS = {"claude": "Claude", "codex": "Codex"}
_AGENT_BACKEND = re.compile(r"(:\s*)Claude(?=\s*[,\]])")


def render(source: str, backend: str) -> str:
    """The program with every agent on `backend`. Sources are written for Claude."""
    return _AGENT_BACKEND.sub(lambda m: m.group(1) + BACKENDS[backend], source)
