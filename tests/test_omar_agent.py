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


class MissionContainer(Container):
    """A container whose OMAR session is the stand-in Mission Control of test_mission."""

    def __init__(self, session):
        super().__init__()
        self.session = session
        session.files = self.files

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.execs.append(SimpleNamespace(command=command, env=env or {}, user=user))
        out = ""
        if command.endswith("run.sh up"):
            out = "url=http://127.0.0.1:4100\nwork=/w\ncwd=/app\n"
        elif command.endswith("run.sh down"):
            out = 'Output t.answer = "done"\n'
        elif "curl" in command:
            out = await self.session.shell(command.removeprefix("set -o pipefail; "))
        return SimpleNamespace(return_code=0, stdout=out, stderr="")


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
        # Already on the system path: an npm install on Alpine.
        for container, backend in ((Container(stdout="/usr/local/bin/claude\n"), "claude"),
                                   (Container(stdout="/usr/local/bin/codex\n"), "codex")):
            self.container = container
            asyncio.run(self.agent(backend=backend).install(container))
            self.assertFalse([e for e in container.execs
                              if "ln -sf" in e.command or "> /usr/local/bin/" in e.command], backend)

    def test_codex_gets_a_launcher_that_loads_its_own_node_as_harbors_agent_does(self):
        # Harbor's link step runs without nvm and links nothing; `sh -lc` then has no codex.
        nvm = "/root/.nvm/versions/node/v22.20.0/bin"
        container = Container(stdout=f"{nvm}/codex\n")
        asyncio.run(self.agent(backend="codex").install(container))
        lookup = next(e for e in container.execs if "command -v codex" in e.command)
        self.assertIn('. "$HOME/.nvm/nvm.sh"', lookup.command)
        (written,) = [e for e in container.execs if "> /usr/local/bin/codex" in e.command]
        self.assertEqual(written.user, "root")
        self.assertIn(f'PATH={nvm}:"$PATH"; export PATH', written.command)
        self.assertIn(f'exec {nvm}/codex "$@"', written.command)
        # node itself is not put on the system path, where the task's tests would see it.
        self.assertFalse([e for e in container.execs if "/usr/local/bin/node" in e.command])

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
            "model": "gpt-5.6-sol", "check_for_update_on_startup": False,
            "model_reasoning_effort": "high", "web_search": "disabled",
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


    def mission(self, backend="sol"):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (tmp / "omar.tar.gz").write_text("a build")
        (tmp / "spec.md").write_text("# OMAR language\n")
        cli = "codex" if backend == "sol" else "claude"
        agent = omar_agent.OmarAgent(
            logs_dir=self.logs, model_name="openai/gpt-6.1-sol" if cli == "codex" else
            "anthropic/claude-sonnet-5-5", backend=cli, brief=str(ROOT / "topologies/mission-control.md"),
            max_mutations="3", omar_version="main-03633ed", omar_sha256="f" * 64,
            omar_tarball=str(tmp / "omar.tar.gz"), omar_spec=str(tmp / "spec.md"),
            cli_version="0.159.0", invocation_timeout_sec="28800",
            extra_env={"OPENAI_API_KEY": "key"})

        async def cli_install(environment):
            self.container.execs.append(SimpleNamespace(command="<cli install>", env={}, user=None))
        agent._cli.install = cli_install
        return agent

    def test_mission_control_installs_the_build_of_its_commit_and_its_reference(self):
        asyncio.run(self.mission().install(self.container))
        first = self.container.execs[0]
        self.assertEqual(first.env["OMAR_TARBALL"], "/tmp/omar-bench-omar.tar.gz")
        self.assertEqual(self.container.files["/tmp/omar-bench-omar.tar.gz"], "a build")
        self.assertEqual(self.container.files[OB + "spec.md"], "# OMAR language\n")
        self.assertNotIn(OB + "topology.omar", self.container.files)
        self.assertEqual(self.mission().version(), "main-03633ed")

    def test_it_runs_a_topology_or_a_brief_never_both(self):
        for kwargs in ({}, {"topology": str(ROOT / "topologies/Solo.omar"),
                            "brief": str(ROOT / "topologies/mission-control.md")}):
            with self.assertRaises(ValueError):
                omar_agent.OmarAgent(logs_dir=self.logs, model_name="anthropic/claude-opus-5-5",
                                     omar_version="x", invocation_timeout_sec="1", **kwargs)

    def test_mission_control_is_brought_up_driven_and_brought_down(self):
        from tests.test_mission import DESIGN, Session, said
        session = Session([said("Here it is.", DESIGN)], runs=[{"status": "completed",
                          "outputs": {"t.answer": "done"}}], snapshots=[{"ports": [], "reactions": []}])
        container = MissionContainer(session)
        self.container = container
        agent = self.mission()
        asyncio.run(agent.run("Fix the parser.", container, AgentContext()))

        steps = [e.command.split("run.sh")[-1].strip() for e in container.execs if "run.sh" in e.command]
        self.assertEqual(steps, ["up", "down"])
        brief = session.sent()[0]
        # The brief, filled in with where this attempt's task is.
        for expected in ("/w/instruction.md", "/app", "/w/spec.md", "`Codex` agent", "8 hours"):
            self.assertIn(expected, brief)
        self.assertNotRegex(brief, r"@[A-Z_]+@")
        # Credentials go to the session's start only; the runtime keeps them from there.
        up, *later = [e for e in container.execs if e.command != "<cli install>"]
        self.assertEqual(up.env["OPENAI_API_KEY"], "key")
        self.assertTrue(later)
        self.assertTrue(all(set(e.env) <= {"OB_BACKEND", "OB_MODEL", "OB_LOGS",
                                           "OB_INVOCATION_TIMEOUT"} for e in later))
        written = json.loads((self.logs / "trajectory.json").read_text())
        self.assertEqual(written["steps"][-1]["message"], "done")
        self.assertTrue((self.logs / "mission-control/design-1.omar").exists())


class RunScript(unittest.TestCase):
    """container/run.sh, executed for real against a stand-in `omar`."""

    def _run(self, omar, stage=None, before=None, **env):
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
        if stage == "up":
            (ob / "spec.md").write_text("# OMAR language\n")
        if before:
            before(tmp, logs)
        # Every call is logged; only `run` is the run, the session commands just succeed
        # (`up` printing what the runtime says of itself, when asked).
        (bin_ / "omar").write_text('#!/bin/sh\necho "$*" >> "$RECORD.calls"\n'
                                   'case " $* " in *" run "*) ;; *" up "*) printf "%s\\n" "${UP_JSON:-}"; exit 0 ;;'
                                   ' *) exit 0 ;; esac\n' + omar)
        (bin_ / "omar").chmod(0o755)
        if env.get("OB_BACKEND") == "codex":
            (ob / "codex-config.toml").write_text('model = "gpt-6-luna"\n')
        ob.chmod(0o555)
        self.addCleanup(ob.chmod, 0o755)
        run = subprocess.run(
            ["sh", str(ROOT / "omar_bench/agents/container/run.sh"), *([stage] if stage else [])],
            cwd=task,
            capture_output=True, text=True, timeout=60, env={
                "PATH": os.environ["PATH"], "HOME": str(tmp), "TMPDIR": str(tmp),
                "OB_DIR": str(ob), "OB_BACKEND": "claude", "OB_MODEL": "claude-haiku-5-5",
                "OB_LOGS": str(logs), "OB_INVOCATION_TIMEOUT": "28800", "RECORD": str(tmp / "rec"),
                "OB_LINK_ROOT": str(tmp), **env})
        return run, tmp, ob, logs, task

    def test_it_runs_where_the_agent_user_cannot_write_to_the_install_directory(self):
        run, tmp, ob, logs, task = self._run(
            'printf "%s\\n" "$PWD" "$@" > "$RECORD"\n'
            # An agent that followed OMAR's own instructions and kept its work in its workspace.
            'w="$HOME/.omar/sessions/bench/workspaces/w1/worktree"; mkdir -p "$w"\n'
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
        # Waits for the run, keeps its workspaces to list them, takes no checkpoints.
        self.assertEqual(args[args.index("--checkpoint-period") + 1], "off")
        self.assertLessEqual({"--fg", "--keep-workspaces"}, set(args))
        # In a session of the attempt's own, made first and removed last.
        up, run_call, rm = (tmp / "rec.calls").read_text().splitlines()
        self.assertEqual(up, f"-c {cwd}/config.toml up --no-ea --name bench")
        self.assertTrue(run_call.startswith("-s bench run "), run_call)
        self.assertEqual(rm, "-s bench rm")
        (task_input,) = [a for a in args if a.startswith("t.statement=")]
        self.assertEqual(Path(task_input.removeprefix("t.statement=")).read_text(), "Fix it.\n")
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
                         f"{tmp}/.omar/sessions/bench/workspaces/w1/worktree/solution.py\n")

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
        run, tmp, _, logs, _ = self._run(self._omar_with((self.WORKING, True), (self.DIED, True)),
                                         OB_WATCH_SECS="1")

        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(run.returncode, 70, run.stderr)
        self.assertIn("ended in an API error", run.stdout)
        # Ending `omar run --fg` leaves the run going; removing the session ends it.
        self.assertEqual((tmp / "rec.calls").read_text().splitlines()[-1], "-s bench rm")
        self.assertEqual((logs / "omar-stall.txt").read_text().splitlines()[-1],
                         str(logs / "sessions/projects/p/s1.jsonl"))

    def test_a_safety_refusal_is_named_as_it_is_for_solo(self):
        # What Claude Code wrote when Haiku 5.5's classifier declined atrx-vep-crispr (2026-10-22).
        refused = json.dumps({"type": "assistant", "error": "invalid_request", "isApiErrorMessage": True,
                              "message": {"content": [{"type": "text", "text":
                                  "API Error: Haiku 5.5 can't help with this. Start a new session to continue."
                                  "\n\nLearn more: https://www.anthropic.com/legal/aup\n\nDetails: `[bio]`"}]}},
                             separators=(",", ":"))  # compact, as Claude Code writes it
        fixture = Path(self.enterContext(tempfile.TemporaryDirectory())) / "refused.jsonl"
        fixture.write_text(refused + '\n{"type":"system","subtype":"turn_duration"}\n')
        run, _, _, logs, _ = self._run(
            self._omar_with((self.WORKING, True),
                            then='cp "$FIXTURE" "$d/s1.jsonl"; touch -t 202601010000 "$d/s1.jsonl"\n'
                                 'exec sleep 40\n'),
            OB_WATCH_SECS="1", FIXTURE=str(fixture))

        self.assertEqual(run.returncode, 70, run.stderr)
        stall = (logs / "omar-stall.txt").read_text().splitlines()
        self.assertIn("can't help with this", "\n".join(stall))
        self.assertEqual(stall[-1], str(logs / "sessions/projects/p/s1.jsonl"))
        # Harbor's own classifier, on what the script printed, as it runs on `solo`'s output.
        agent = omar_agent.OmarAgent(
            logs_dir=logs, model_name="anthropic/claude-haiku-5-5", backend="claude",
            topology=str(ROOT / "topologies/PlanBuildCheck.omar"), omar_version="0.4.3",
            omar_sha256="f" * 64, cli_version="2.1.293", invocation_timeout_sec="28800")
        error = agent._classify_exec_error(
            "sh run.sh", SimpleNamespace(return_code=70, stdout=run.stdout, stderr=run.stderr))
        self.assertEqual(type(error).__name__, "AgentSafetyRefusalError")

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

    # A turn Claude Code finished, as it leaves it.
    ANSWERED = ('{"type":"user","message":{"content":"go"}}\\n'
                '{"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}\\n'
                '{"type":"system","subtype":"turn_duration"}\\n{"type":"last-prompt"}\\n')

    def test_a_step_no_agent_will_finish_ends_the_attempt(self):
        # Every turn is over and nobody writes: one agent ended its turn without completing
        # its OMAR step, and OMAR would wait for it until the time limit.
        run, _, _, logs, _ = self._run(
            self._omar_with((self.ANSWERED, True), (self.ANSWERED, True)), OB_WATCH_SECS="1")

        self.assertEqual(run.returncode, 70, run.stderr)
        stall = (logs / "omar-stall.txt").read_text()
        self.assertIn("every agent's turn was over", stall)
        self.assertIn(str(logs / "sessions/projects/p/s1.jsonl"), stall)

    # Codex rollouts: every task_started is closed by a task_complete once the turn is over.
    CODEX_DONE = ('{"type":"event_msg","payload":{"type":"task_started"}}\\n'
                  '{"type":"event_msg","payload":{"type":"task_complete"}}\\n')
    CODEX_IN_TURN = CODEX_DONE + '{"type":"event_msg","payload":{"type":"task_started"}}\\n'

    def _codex_with(self, *rollouts, then="exec sleep 40\n"):
        script = 'd="$CODEX_HOME/sessions/2026/10/14"; mkdir -p "$d"\n'
        for n, (text, old) in enumerate(rollouts):
            script += f"printf '{text}' > \"$d/rollout-{n}.jsonl\"\n"
            script += f'touch -t 202601010000 "$d/rollout-{n}.jsonl"\n' if old else ""
        return script + then

    def test_codex_agents_are_watched_the_same_way(self):
        codex = {"OB_BACKEND": "codex", "OB_MODEL": "gpt-6-luna", "OB_WATCH_SECS": "1"}
        started = time.monotonic()
        run, _, _, logs, _ = self._run(
            self._codex_with((self.CODEX_DONE, True), (self.CODEX_DONE, True)), **codex)

        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(run.returncode, 70, run.stderr)
        stall = (logs / "omar-stall.txt").read_text()
        self.assertIn("every agent's turn was over", stall)
        self.assertIn(str(logs / "sessions/2026/10/14/rollout-1.jsonl"), stall)

        # A turn still open, however quiet, is an agent at work: a long build, say.
        run, _, _, logs, _ = self._run(
            self._codex_with((self.CODEX_DONE, True), (self.CODEX_IN_TURN, True),
                             then='sleep 4; echo done\n'), **codex)

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse((logs / "omar-stall.txt").exists())


    UP = '{\n  "name": "bench",\n  "url": "http://127.0.0.1:4100",\n  "state": "ready"\n}'

    def test_up_starts_a_session_with_mission_controls_assistant_on_the_arms_cli(self):
        for backend, assistant in (("claude", "claude --dangerously-skip-permissions"),
                                   ("codex", "codex --dangerously-bypass-approvals-and-sandbox")):
            run, tmp, ob, logs, task = self._run("", stage="up", UP_JSON=self.UP, OB_BACKEND=backend)
            self.assertEqual(run.returncode, 0, run.stderr)
            said = dict(line.split("=", 1) for line in run.stdout.splitlines())
            self.assertEqual(said["url"], "http://127.0.0.1:4100")
            self.assertEqual(said["cwd"], str(task))
            work = Path(said["work"])
            # With its assistant this time; nothing is run yet: the adapter drives it from here.
            (up,) = (tmp / "rec.calls").read_text().splitlines()
            self.assertEqual(up, f"-c {work}/config.toml up --name bench --json")
            self.assertIn(f'[agent]\ndefault_command = "{assistant}"', (work / "config.toml").read_text())
            self.assertEqual((work / "spec.md").read_text(), "# OMAR language\n")
            self.assertEqual((work / "instruction.md").read_text(), "Fix it.\n")

    def test_up_without_an_address_is_an_error(self):
        run, *_ = self._run("", stage="up", UP_JSON="Started")
        self.assertEqual(run.returncode, 1)
        self.assertIn("omar up gave no address", run.stdout)

    def test_stalled_says_why_and_leaves_the_reason_where_the_queue_reads_it(self):
        def transcripts(tmp, logs, old=True):
            d = logs / "sessions/projects/p"
            d.mkdir(parents=True)
            for n in range(2):
                (d / f"s{n}.jsonl").write_text(self.ANSWERED.replace("\\n", "\n"))
                if old:
                    os.utime(d / f"s{n}.jsonl", (1767225600, 1767225600))

        run, _, _, logs, _ = self._run("", stage="stalled", before=transcripts)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("every agent's turn was over", run.stdout)
        self.assertIn("every agent's turn was over", (logs / "omar-stall.txt").read_text())
        run, _, _, logs, _ = self._run("", stage="stalled",
                                       before=lambda tmp, logs: transcripts(tmp, logs, old=False))
        self.assertEqual((run.returncode, run.stdout), (0, ""))
        self.assertFalse((logs / "omar-stall.txt").exists())

    def test_down_ends_the_session_and_says_how_the_attempt_ended(self):
        for left, code in ((None, 0), ("omar-stall.txt", 70), ("omar-mission-failed.txt", 71)):
            def leave(tmp, logs):
                (logs / "omar-run.txt").write_text('Output t.answer = "ok"\n')
                if left:
                    (logs / left).write_text(f"omar-bench ended this attempt: {left}\n")
            run, tmp, _, logs, _ = self._run("", stage="down", before=leave)
            self.assertEqual(run.returncode, code, left)
            self.assertIn('Output t.answer = "ok"', run.stdout)
            self.assertEqual((tmp / "rec.calls").read_text().splitlines(), ["-s bench rm"])
            self.assertTrue((logs / "omar-workspace-files.txt").exists())

    def test_down_turns_links_into_a_workspace_into_the_files_they_point_at(self):
        def leave(tmp, logs):
            w = tmp / ".omar/sessions/bench/workspaces/w1/worktree"
            (w / "out").mkdir(parents=True)
            (w / "answers.csv").write_text("Answers\n42\n")
            (w / "out/rules.json").write_text("[]\n")
            (tmp / "results").mkdir()
            (tmp / "results/answers.csv").symlink_to(w / "answers.csv")
            (tmp / "task/out").symlink_to(w / "out", target_is_directory=True)
            # A link the task came with, to somewhere else, stays a link.
            (tmp / "task/data").symlink_to(tmp / "results")
        run, tmp, _, logs, task = self._run("", stage="down", before=leave)

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse((tmp / "results/answers.csv").is_symlink())
        self.assertEqual((tmp / "results/answers.csv").read_text(), "Answers\n42\n")
        self.assertFalse((task / "out").is_symlink())
        self.assertEqual((task / "out/rules.json").read_text(), "[]\n")
        self.assertTrue((task / "data").is_symlink())
        w = tmp / ".omar/sessions/bench/workspaces/w1/worktree"
        self.assertEqual(sorted((logs / "omar-linked-files.txt").read_text().splitlines()),
                         [f"{tmp}/results/answers.csv -> {w}/answers.csv", f"{task}/out -> {w}/out"])
        self.assertEqual(sorted(p.name for p in (tmp / "results").iterdir()), ["answers.csv"])


if __name__ == "__main__":
    unittest.main()
