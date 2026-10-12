"""The Harbor adapter, driven against a recording stand-in for the task container.

Needs Harbor importable (`uv run --with harbor python -m unittest ...`); skipped otherwise.
No container is started: this checks what the adapter uploads and executes, not
that OMAR then runs.
"""
import asyncio
import json
import os
import subprocess
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    from harbor.models.agent.context import AgentContext
    from harbor.models.task.config import MCPServerConfig
    from harbor.models.trajectories import Trajectory

    from omar_bench.agents import omar_agent
except ImportError:
    omar_agent = None

from omar_bench.config import ROOT

OB = "/installed-agent/omar-bench/"


class Container:
    """Records every upload and exec the adapter performs."""

    default_user = "agent"
    capabilities = SimpleNamespace(mounted=True)

    def __init__(self, stdout="", fail=False):
        self.files: dict[str, str] = {}
        self.execs: list[SimpleNamespace] = []
        self.stdout, self.fail = stdout, fail

    async def upload_file(self, source_path, target_path):
        self.files[target_path] = Path(source_path).read_text()

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.execs.append(SimpleNamespace(command=command, env=env or {}, user=user))
        return SimpleNamespace(return_code=1 if self.fail else 0, stdout=self.stdout, stderr="")


@unittest.skipIf(omar_agent is None, "harbor is not installed")
class OmarAgent(unittest.TestCase):
    def setUp(self):
        self.logs = Path(self.enterContext(tempfile.TemporaryDirectory())) / "trial-1" / "agent"
        self.logs.mkdir(parents=True)
        self.container = Container()

    def agent(self, backend="claude", model="anthropic/claude-opus-5-5", **kwargs):
        kwargs.setdefault("invocation_timeout_sec", "28800")  # --ak values arrive as text
        agent = omar_agent.OmarAgent(
            logs_dir=self.logs, model_name=model, backend=backend,
            topology=str(ROOT / "topologies/PlanBuildCheck.omar"),
            omar_version="0.4.2", omar_sha256="f" * 64, cli_version="2.1.293", **kwargs)

        async def cli_install(environment):  # Harbor's own installer; not under test here
            self.container.execs.append(SimpleNamespace(command="<cli install>", env={}, user=None))
        agent._cli.install = cli_install
        return agent

    def test_install_puts_the_pinned_runtime_then_the_cli_then_the_program(self):
        agent = self.agent(mcp_servers=[
            MCPServerConfig(name="world", transport="streamable-http", url="http://world:8000/mcp/")])
        asyncio.run(agent.install(self.container))

        first, second = self.container.execs[:2]
        self.assertIn("omar-bench-install-root.sh", first.command)
        self.assertEqual(first.user, "root")
        self.assertEqual(first.env, {"OMAR_REPO": "omar-os/omar", "OMAR_VERSION": "0.4.2",
                                     "OMAR_SHA256": "f" * 64})
        self.assertEqual(second.command, "<cli install>")
        self.assertEqual(agent._cli.version(), "2.1.293")

        self.assertIn("main Bench", self.container.files[OB + "topology.omar"])
        self.assertEqual(json.loads(self.container.files[OB + "claude-settings.json"]),
                         {"model": "claude-opus-5-5"})
        claude = json.loads(self.container.files[OB + "claude.json"])
        self.assertTrue(claude["hasCompletedOnboarding"])
        # The agents' starting directory, which run.sh fills in, is trusted up front.
        self.assertEqual(claude["projects"], {"@WORKSPACE@": {"hasTrustDialogAccepted": True}})
        # The task's own tools (an APEX world's MCP gateway) reach every agent on the team.
        self.assertEqual(claude["mcpServers"],
                         {"world": {"type": "http", "url": "http://world:8000/mcp/"}})
        # What this attempt ran is kept beside its logs.
        self.assertTrue((self.logs / "omar-bench/topology.omar").exists())

    def test_claude_code_is_linked_where_omars_login_shell_finds_it(self):
        # Harbor installs it under the agent user's home; `sh -lc`, which OMAR uses, does not look there.
        self.container.stdout = "/root/.local/bin/claude\n"
        asyncio.run(self.agent().install(self.container))
        (linked,) = [e for e in self.container.execs if "ln -sf" in e.command]
        self.assertTrue(linked.command.endswith(
            "ln -sf /root/.local/bin/claude /usr/local/bin/claude"), linked.command)
        self.assertEqual(linked.user, "root")
        # Already on the system path (an npm install on Alpine), or Codex, which Harbor links itself.
        for container, backend in ((Container(stdout="/usr/local/bin/claude\n"), "claude"),
                                   (Container(stdout="/root/.nvm/bin/codex\n"), "codex")):
            self.container = container
            asyncio.run(self.agent(backend=backend).install(container))
            self.assertFalse([e for e in container.execs if "ln -sf" in e.command], backend)

    def test_the_suites_cli_settings_reach_a_claude_team(self):
        agent = self.agent(reasoning_effort="high", disallowed_tools="WebSearch,WebFetch")
        asyncio.run(agent.install(self.container))
        self.assertEqual(json.loads(self.container.files[OB + "claude-settings.json"]), {
            "model": "claude-opus-5-5", "permissions": {"deny": ["WebSearch", "WebFetch"]}})
        asyncio.run(agent.run("task", self.container, AgentContext()))
        self.assertEqual(self.container.execs[-1].env["CLAUDE_CODE_EFFORT_LEVEL"], "high")

    def test_a_codex_team_is_rendered_and_configured_for_codex(self):
        agent = self.agent(backend="codex", model="openai/gpt-5.6-sol",
                           reasoning_effort="high", web_search="disabled", mcp_servers=[
            MCPServerConfig(name="fs", transport="stdio", command="fs-server", args=["--ro"])])
        asyncio.run(agent.install(self.container))
        self.assertEqual(self.container.files[OB + "topology.omar"].count(": Codex]"), 3)
        self.assertEqual(tomllib.loads(self.container.files[OB + "codex-config.toml"]), {
            "model": "gpt-5.6-sol", "model_reasoning_effort": "high", "web_search": "disabled",
            "mcp_servers": {"fs": {"command": "fs-server", "args": ["--ro"]}}})

    def test_run_hands_over_the_task_text_and_only_this_backends_credentials(self):
        agent = self.agent(extra_env={
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "ANTHROPIC_API_KEY": "key",
            "CLAUDE_FORCE_OAUTH": "1", "OPENAI_API_KEY": "judge-key"})
        asyncio.run(agent.run("Fix the slow payment service.", self.container, AgentContext()))

        self.assertEqual(self.container.files[OB + "instruction.md"],
                         "Fix the slow payment service.")
        (run,) = self.container.execs
        self.assertTrue(run.command.endswith("sh /installed-agent/omar-bench/run.sh"))
        self.assertEqual(run.env, {
            # Subscription forced: the API key (there for a grader) is withheld from the agents.
            "CLAUDE_CODE_OAUTH_TOKEN": "oauth",
            "OB_BACKEND": "claude", "OB_MODEL": "claude-opus-5-5",
            "OB_LOGS": "/logs/agent", "OB_INVOCATION_TIMEOUT": "28800"})

    def test_it_cannot_be_built_without_the_suites_time_limit(self):
        # A default would quietly stop OMAR's agents before the suite stops a solo agent.
        with self.assertRaises(TypeError):
            omar_agent.OmarAgent(logs_dir=self.logs, model_name="anthropic/claude-opus-5-5",
                                 topology=str(ROOT / "topologies/Solo.omar"), omar_version="0.4.2")

    def test_the_teams_answer_is_left_as_a_trajectory_for_the_suites_grader(self):
        self.container.stdout = ('Topology \'Bench\' completed\n'
                                 'Output t.answer = "EV/EBITDA is 11.4x.\\nSee tab \\"Comps\\"."\n')
        asyncio.run(self.agent().run("What multiple?", self.container, AgentContext()))
        written = json.loads((self.logs / "trajectory.json").read_text())
        steps = Trajectory.model_validate(written).steps  # valid ATIF on this Harbor version
        self.assertEqual([(s.source, s.message) for s in steps], [
            ("user", "What multiple?"), ("agent", 'EV/EBITDA is 11.4x.\nSee tab "Comps".')])
        self.assertEqual(written["session_id"], "trial-1")
        self.assertEqual(written["agent"], {"name": "omar", "version": "0.4.2",
                                            "model_name": "claude-opus-5-5"})

    def test_a_failed_run_still_leaves_a_trajectory_with_no_answer(self):
        self.container.fail = True
        with self.assertRaises(Exception):
            asyncio.run(self.agent().run("task", self.container, AgentContext()))
        written = json.loads((self.logs / "trajectory.json").read_text())
        self.assertEqual(written["steps"][-1], {"step_id": 2, "source": "agent", "message": ""})

    def test_an_unmounted_sandbox_gets_the_trajectory_uploaded(self):
        self.container.capabilities = SimpleNamespace(mounted=False)
        self.container.stdout = 'Output t.answer = "42"\n'
        asyncio.run(self.agent().run("task", self.container, AgentContext()))
        uploaded = json.loads(self.container.files["/logs/agent/trajectory.json"])
        self.assertEqual(uploaded["steps"][-1]["message"], "42")

    def test_it_declares_that_it_takes_task_mcp_servers_where_harbor_asks(self):
        if omar_agent.AgentCapabilities is None:
            self.skipTest("this Harbor version has no agent capabilities")
        self.assertTrue(omar_agent.OmarAgent.capabilities.mcp_servers)

    def test_the_whole_teams_tokens_are_reported_to_harbor(self):
        for name, out in (("planner", 100), ("builder", 400), ("checker", 50)):
            path = self.logs / "sessions/projects" / name / "s.jsonl"
            path.parent.mkdir(parents=True)
            line = {"type": "assistant", "message": {"id": name, "model": "claude-opus-5-5", "usage": {
                "input_tokens": 10, "cache_read_input_tokens": 1000, "output_tokens": out}}}
            path.write_text(json.dumps(line) + "\n" + json.dumps(line) + "\n")
        context = AgentContext()
        self.agent().populate_context_post_run(context)
        self.assertEqual((context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens),
                         (3030, 3000, 550))
        self.assertEqual(context.metadata["omar_bench_usage"]["claude-opus-5-5"]["requests"], 3)


class RunScript(unittest.TestCase):
    """container/run.sh, executed for real against a stand-in `omar`."""

    def _run(self, omar, **env):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        # The script puts $HOME/.local/bin first, so the stand-in wins over any installed omar.
        ob, logs, bin_, task = tmp / "ob", tmp / "logs", tmp / ".local/bin", tmp / "task"
        for directory in (ob, logs, bin_, task):
            directory.mkdir(parents=True)
        (ob / "topology.omar").write_text("main Bench { t = Team() }\n")
        (ob / "instruction.md").write_text("Fix it.\n")
        (ob / "claude.json").write_text('{"projects": {"@WORKSPACE@": {"hasTrustDialogAccepted": true}}}')
        # Left by the CLI's installer; OMAR will look for sessions here and nowhere else.
        (tmp / ".claude/downloads").mkdir(parents=True)
        (ob / "claude-settings.json").write_text("{}")
        (bin_ / "omar").write_text("#!/bin/sh\n" + omar)
        (bin_ / "omar").chmod(0o755)
        ob.chmod(0o555)
        self.addCleanup(ob.chmod, 0o755)
        run = subprocess.run(
            ["sh", str(ROOT / "omar_bench/agents/container/run.sh")], cwd=task,
            capture_output=True, text=True, timeout=60, env={
                "PATH": os.environ["PATH"], "HOME": str(tmp), "TMPDIR": str(tmp),
                "OB_DIR": str(ob), "OB_BACKEND": "claude", "OB_MODEL": "claude-haiku-5-5",
                "OB_LOGS": str(logs), "OB_INVOCATION_TIMEOUT": "28800", "RECORD": str(tmp / "rec"),
                **env})
        return run, tmp, ob, logs, task

    def test_it_runs_where_the_agent_user_cannot_write_to_the_install_directory(self):
        run, tmp, ob, logs, task = self._run(
            'printf "%s\\n" "$PWD" "$@" > "$RECORD"\n'
            # An agent that followed OMAR's own instructions and kept its work in its workspace.
            'w="$HOME/.omar/workspaces/w1/worktree"; mkdir -p "$w"\n'
            'echo pointer > "$w/.git"; echo work > "$w/solution.py"\n'
            # What the CLI writes: a transcript, and files of its own.
            'mkdir -p "$HOME/.claude/projects/p" "$HOME/.claude/plugins"\n'
            'echo "{}" > "$HOME/.claude/projects/p/s.jsonl"; echo x > "$HOME/.claude/plugins/big"\n'
            'echo \'Output t.answer = "ok"\'\n')

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('Output t.answer = "ok"', run.stdout)
        cwd, *args = (tmp / "rec").read_text().splitlines()
        self.assertNotEqual(cwd, str(ob))
        self.assertIn(f"t.cwd={task}", args)
        self.assertEqual(args[args.index("--timeout-seconds") + 1], "28800")
        (task_input,) = [a for a in args if a.startswith("t.task=")]
        self.assertEqual(Path(task_input.removeprefix("t.task=")).read_text(), "Fix it.\n")
        # Nothing was added beside the uploads.
        self.assertEqual(sorted(p.name for p in ob.iterdir()),
                         ["claude-settings.json", "claude.json", "instruction.md", "topology.omar"])
        # The CLI's config directory is its default one. Of what it writes there, only the
        # transcripts land where Harbor syncs.
        self.assertFalse((tmp / ".claude").is_symlink())
        self.assertTrue((tmp / ".claude/settings.json").exists())
        self.assertTrue((tmp / ".claude/downloads").is_dir())
        self.assertEqual([str(p.relative_to(logs / "sessions")) for p in sorted((logs / "sessions").rglob("*"))],
                         ["projects", "projects/p", "projects/p/s.jsonl"])
        self.assertEqual(json.loads((tmp / ".claude.json").read_text()),
                         {"projects": {cwd: {"hasTrustDialogAccepted": True}}})
        self.assertTrue((logs / "omar-run.txt").exists())
        self.assertFalse((logs / "omar-stall.txt").exists())
        # Work left in an OMAR workspace, where no grader looks, is on record.
        self.assertEqual((logs / "omar-workspace-files.txt").read_text(),
                         f"{tmp}/.omar/workspaces/w1/worktree/solution.py\n")

    # A transcript as Claude Code leaves it: the last thing said, then its own bookkeeping.
    WORKING = ('{"type":"user","message":{"content":"go"}}\\n'
               '{"type":"assistant","message":{"content":[{"type":"text","text":"on it"}]}}\\n'
               '{"type":"mode","mode":"normal"}\\n')
    DIED = ('{"type":"user","message":{"content":"go"}}\\n'
            '{"type":"assistant","error":"max_output_tokens","isApiErrorMessage":true,"message":{}}\\n'
            '{"type":"system","subtype":"turn_duration"}\\n{"type":"last-prompt"}\\n')

    def _omar_with(self, *transcripts, then="exec sleep 40\n"):
        """A stand-in whose agents have written these (text, backdated?) transcripts."""
        script = 'd="$HOME/.claude/projects/p"; mkdir -p "$d"\n'
        for n, (text, old) in enumerate(transcripts):
            script += f"printf '{text}' > \"$d/s{n}.jsonl\"\n"
            script += f'touch -t 202601010000 "$d/s{n}.jsonl"\n' if old else ""
        return script + then

    def test_an_agent_that_died_on_an_api_error_ends_the_attempt_instead_of_the_time_limit(self):
        # OMAR goes on waiting for it; here that would be 40 seconds, and for real 8 hours.
        started = time.monotonic()
        run, _, _, logs, _ = self._run(self._omar_with((self.WORKING, True), (self.DIED, True)),
                                       OB_WATCH_SECS="1")

        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(run.returncode, 70, run.stderr)
        self.assertIn("ended in an API error", run.stdout)
        self.assertEqual((logs / "omar-stall.txt").read_text().splitlines()[-1],
                         str(logs / "sessions/projects/p/s1.jsonl"))

    def test_a_dead_turn_ends_nothing_while_another_agent_is_still_writing(self):
        run, _, _, logs, _ = self._run(
            self._omar_with((self.DIED, True), (self.WORKING, False), then='sleep 4; echo done\n'),
            OB_WATCH_SECS="1")

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse((logs / "omar-stall.txt").exists())

    def test_agents_that_are_only_quiet_are_left_to_work(self):
        # A long tool call or a long response looks like this, and so does a stuck agent
        # with no error on record. Neither is ended: only the time limit does that.
        run, _, _, logs, _ = self._run(
            self._omar_with((self.WORKING, True), (self.WORKING, True), then='sleep 4; echo done\n'),
            OB_WATCH_SECS="1")

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse((logs / "omar-stall.txt").exists())


if __name__ == "__main__":
    unittest.main()
