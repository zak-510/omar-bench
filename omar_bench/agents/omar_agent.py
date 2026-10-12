"""OMAR as a Harbor agent: run a topology inside the task container.

Terminal-Bench, APEX-Agents and SWE Atlas all publish on Harbor, so this one
adapter puts an OMAR team on all three:

    harbor run -d <dataset> -a omar_bench.agents.omar_agent:OmarAgent -m anthropic/<model> \
        --ak topology=topologies/PlanBuildCheck.omar --ak omar_version=<pin> ...

`omar-bench run` builds that command from bench.toml.
"""
from __future__ import annotations

import json
import os
import shlex
from pathlib import Path, PurePosixPath

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.agents.installed.claude_code import ClaudeCode
from harbor.agents.installed.codex import Codex
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.models.trial.paths import EnvironmentPaths

try:
    from harbor.agents.capabilities import AgentCapabilities
except ImportError:  # harbor < 0.23, which APEX-Agents and SWE Atlas pin
    AgentCapabilities = None

from .. import usage
from ..topology import render

HERE = Path(__file__).resolve().parent / "container"
OB = PurePosixPath("/installed-agent/omar-bench")
CODEX_SECRETS = PurePosixPath("/tmp/codex-secrets")
# Harbor's own integration installs the CLI, so every arm gets it the same way.
CLI = {"claude": ClaudeCode, "codex": Codex}
CREDENTIALS = {
    "claude": ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
               "ANTHROPIC_BASE_URL"),
    "codex": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
}


def claude_json(mcp_servers) -> str:
    """User-scope Claude Code config: first-run dialogs answered, the task's MCP servers attached.

    `run.sh` puts the directory the agents start in where @WORKSPACE@ is.
    """
    servers = {}
    for server in mcp_servers:
        if server.transport == "stdio":
            servers[server.name] = {"type": "stdio", "command": server.command, "args": server.args}
        else:
            kind = "http" if server.transport == "streamable-http" else server.transport
            servers[server.name] = {"type": kind, "url": server.url}
    return json.dumps({"hasCompletedOnboarding": True, "bypassPermissionsModeAccepted": True,
                       "projects": {"@WORKSPACE@": {"hasTrustDialogAccepted": True}},
                       "mcpServers": servers}, indent=2)


def claude_settings(model: str, disallowed_tools) -> str:
    settings: dict = {"model": model}
    if disallowed_tools:
        tools = disallowed_tools.split(",") if isinstance(disallowed_tools, str) else disallowed_tools
        settings["permissions"] = {"deny": [tool.strip() for tool in tools]}
    return json.dumps(settings)


def codex_config(model: str, mcp_servers, reasoning_effort=None, web_search=None) -> str:
    lines = [f"model = {json.dumps(model)}"]
    if reasoning_effort:
        lines.append(f"model_reasoning_effort = {json.dumps(reasoning_effort)}")
    if web_search:
        lines.append(f"web_search = {json.dumps(web_search)}")
    for server in mcp_servers:
        lines += ["", f"[mcp_servers.{json.dumps(server.name)}]"]
        if server.transport == "stdio":
            lines += [f"command = {json.dumps(server.command)}", f"args = {json.dumps(server.args)}"]
        else:
            lines += [f"url = {json.dumps(server.url)}"]
    return "\n".join(lines) + "\n"


def answer(omar_output: str) -> str:
    """The team's final response: what the topology wrote to `t.answer`."""
    for line in omar_output.splitlines():
        if line.startswith("Output t.answer = "):
            value = line.removeprefix("Output t.answer = ")
            try:
                return str(json.loads(value))
            except json.JSONDecodeError:
                return value
    return ""


class OmarAgent(BaseInstalledAgent):
    # Newer Harbor refuses a task that brings MCP servers unless the agent says it takes them.
    if AgentCapabilities is not None:
        capabilities = AgentCapabilities(atif=True, mcp_servers=True)

    def __init__(self, logs_dir: Path, *args, topology: str, omar_version: str,
                 omar_repo: str = "omar-os/omar", omar_sha256: str = "", backend: str = "claude",
                 cli_version: str | None = None, invocation_timeout_sec: int,
                 reasoning_effort: str | None = None, disallowed_tools=None,
                 web_search: str | None = None,
                 extra_env: dict[str, str] | None = None, **kwargs):
        super().__init__(logs_dir, *args, extra_env=extra_env, **kwargs)
        self._topology = Path(topology)
        self._omar = {"OMAR_REPO": omar_repo, "OMAR_VERSION": str(omar_version),
                      "OMAR_SHA256": str(omar_sha256)}
        self._backend = backend
        # `omar run --timeout-seconds`: the limit on one agent's turn, not on the attempt.
        # No default: a short one would stop OMAR's agents before the suite stops anyone else's.
        self._invocation_timeout_sec = int(invocation_timeout_sec)
        # The suite's CLI settings; the same values Harbor's own agent gets on the solo arm.
        self._reasoning_effort = reasoning_effort
        self._disallowed_tools = disallowed_tools
        self._web_search = web_search
        self._env = {**os.environ, **(extra_env or {})}
        self._cli = CLI[backend](logs_dir=logs_dir, model_name=self.model_name,
                                 version=str(cli_version) if cli_version else None)
        # Kept beside the trial's logs: the exact program and task text this attempt ran.
        self._local = Path(logs_dir) / "omar-bench"

    @staticmethod
    def name() -> str:
        return "omar"

    def version(self) -> str | None:
        return self._omar["OMAR_VERSION"]

    @property
    def _model(self) -> str:
        return (self.model_name or "").split("/")[-1]

    @property
    def _container_logs(self) -> PurePosixPath:
        # Harbor 0.20 (which APEX-Agents pins) predates the per-agent override.
        return getattr(self, "environment_logs_dir", None) or EnvironmentPaths.agent_dir

    async def _upload(self, environment: BaseEnvironment, name: str, text: str) -> None:
        self._local.mkdir(parents=True, exist_ok=True)
        (self._local / name).write_text(text)
        await environment.upload_file(self._local / name, str(OB / name))

    async def install(self, environment: BaseEnvironment) -> None:
        await environment.upload_file(HERE / "install-root.sh", "/tmp/omar-bench-install-root.sh")
        await self.exec_as_root(environment, command="sh /tmp/omar-bench-install-root.sh",
                                env=self._omar)
        await self._cli.install(environment)
        await self._link_cli(environment)

        await self._upload(environment, "topology.omar",
                           render(self._topology.read_text(), self._backend))
        await environment.upload_file(HERE / "run.sh", str(OB / "run.sh"))
        if self._backend == "claude":
            await self._upload(environment, "claude.json", claude_json(self.mcp_servers))
            await self._upload(environment, "claude-settings.json",
                               claude_settings(self._model, self._disallowed_tools))
        else:
            await self._upload(environment, "codex-config.toml", codex_config(
                self._model, self.mcp_servers, self._reasoning_effort, self._web_search))
            auth = self._codex_auth_json()
            if auth is not None:
                # Same opt-in and same in-container location as Harbor's codex agent.
                await self.exec_as_root(environment, command=f"mkdir -p {CODEX_SECRETS}")
                await environment.upload_file(auth, str(CODEX_SECRETS / "auth.json"))
                await self.exec_as_root(environment, command=f"chmod -R a+rX {CODEX_SECRETS}")

    async def _link_cli(self, environment: BaseEnvironment) -> None:
        """Put the CLI where a login shell finds it.

        OMAR starts each agent with `sh -lc`, and a login shell resets PATH to the
        system's. Harbor leaves Claude Code in the agent user's ~/.local/bin, on PATH
        only for bash; without this OMAR's agent exits with "claude: not found" and
        OMAR reports that it "did not become ready". Harbor links Codex itself.
        """
        if self._backend != "claude":
            return
        found = await self.exec_as_agent(
            environment, command='PATH="$HOME/.local/bin:$PATH" command -v claude')
        path = (found.stdout or "").strip()
        if path and path != "/usr/local/bin/claude":
            await self.exec_as_root(
                environment, command=f"ln -sf {shlex.quote(path)} /usr/local/bin/claude")

    def _codex_auth_json(self) -> Path | None:
        """A ChatGPT-subscription login to inject, under Harbor's own two switches."""
        if self._env.get("CODEX_AUTH_JSON_PATH"):
            return Path(self._env["CODEX_AUTH_JSON_PATH"])
        if self._env.get("CODEX_FORCE_AUTH_JSON", "").lower() in ("1", "true", "yes"):
            return Path.home() / ".codex" / "auth.json"
        return None

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        await self._upload(environment, "instruction.md", instruction)
        env = {name: self._env[name] for name in CREDENTIALS[self._backend]
               if self._env.get(name)}
        if self._env.get("CLAUDE_FORCE_OAUTH", "").lower() in ("1", "true", "yes"):
            env.pop("ANTHROPIC_API_KEY", None)  # otherwise the CLI prefers the key
        if self._backend == "claude" and self._reasoning_effort:
            env["CLAUDE_CODE_EFFORT_LEVEL"] = self._reasoning_effort
        env |= {
            "OB_BACKEND": self._backend,
            "OB_MODEL": self._model,
            "OB_LOGS": str(self._container_logs),
            "OB_INVOCATION_TIMEOUT": str(self._invocation_timeout_sec),
        }
        result = None
        try:
            result = await self.exec_as_agent(environment, command=f"sh {OB / 'run.sh'}", env=env)
        finally:
            # Also on failure or timeout: an attempt with no answer is graded as one, not lost.
            await self._write_trajectory(environment, instruction, result)

    async def _write_trajectory(self, environment: BaseEnvironment, instruction: str,
                                result) -> None:
        """The task and the team's final response, as ATIF.

        APEX-Agents grades from this file; its verifier collects it from the
        agent's log directory.
        """
        log = Path(self.logs_dir) / "omar-run.txt"  # there already when the logs dir is mounted
        output = (result.stdout if result is not None else None) or (
            log.read_text() if log.exists() else "")
        path = Path(self.logs_dir) / "trajectory.json"
        path.write_text(json.dumps({
            "schema_version": "ATIF-v1.5",
            "session_id": Path(self.logs_dir).parent.name,
            "agent": {"name": "omar", "version": self.version(), "model_name": self._model},
            "steps": [{"step_id": 1, "source": "user", "message": instruction},
                      {"step_id": 2, "source": "agent", "message": answer(output)}],
        }, indent=2))
        if not getattr(environment.capabilities, "mounted", True):
            await environment.upload_file(path, str(self._container_logs / "trajectory.json"))

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Team-wide token totals, in Harbor's convention (input includes cached tokens)."""
        by_model = usage.scan(self.logs_dir)
        total = usage.total(by_model)
        cache_write = total["cache_write_5m"] + total["cache_write_1h"]
        context.n_input_tokens = total["input"] + total["cache_read"] + cache_write
        context.n_cache_tokens = total["cache_read"]
        context.n_output_tokens = total["output"]
        context.metadata = {**(context.metadata or {}), "omar_bench_usage": by_model}
