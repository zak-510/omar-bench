"""Omnigent as a Harbor agent: its coding orchestrator, Polly, on the arm's one model.

    harbor run -d <dataset> -a omar_bench.agents.omnigent_agent:OmnigentAgent -m openai/<model> \
        --ak omnigent_version=<pin> --ak brief=topologies/omnigent.md ...

Polly comes as shipped, changed only so that one model does the whole task (see
omar_bench/polly.py). What else the task means for its way of working (graded in place,
nobody to merge, a reviewer on the same model) the brief says. `omar-bench run` builds
that command from bench.toml.
"""
from __future__ import annotations

from pathlib import Path

from harbor.agents.installed.base import with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

try:
    from harbor.agents.capabilities import AgentCapabilities
except ImportError:  # harbor < 0.23
    AgentCapabilities = None

from .. import polly
from ..topology import BACKENDS
from .omar_agent import HERE, OB, CliTeamAgent


class OmnigentAgent(CliTeamAgent):
    if AgentCapabilities is not None:
        capabilities = AgentCapabilities(atif=True)

    def __init__(self, logs_dir: Path, *args, omnigent_version: str, brief: str, **kwargs):
        super().__init__(logs_dir, *args, **kwargs)
        self._version = str(omnigent_version)
        self._brief = Path(brief)

    @staticmethod
    def name() -> str:
        return "omnigent"

    def version(self) -> str | None:
        return self._version

    async def install(self, environment: BaseEnvironment) -> None:
        await environment.upload_file(HERE / "install-omnigent.sh",
                                      "/tmp/omar-bench-install-omnigent.sh")
        await self.exec_as_root(environment, command="sh /tmp/omar-bench-install-omnigent.sh",
                                env={"OMNIGENT_VERSION": self._version})
        await self._cli.install(environment)
        await self._link_cli(environment)
        bundle = polly.render(self._backend, self._model, self._reasoning_effort)
        dirs = sorted({str((OB / "polly" / path).parent) for path in bundle})
        await self.exec_as_root(environment, command="mkdir -p " + " ".join(dirs)
                                + f" && chmod -R a+rwX {OB / 'polly'}")
        for path, text in bundle.items():
            await self._upload(environment, f"polly/{path}", text)
        # run.sh fills in where the statement and the task's directory are.
        await self._upload(environment, "omnigent-brief.md", self._brief.read_text()
                           .replace("@BACKEND@", BACKENDS[self._backend])
                           .replace("@MODEL@", self._model))
        await environment.upload_file(HERE / "run.sh", str(OB / "run.sh"))
        await environment.upload_file(HERE / "omnigent_watch.py", str(OB / "omnigent_watch.py"))
        await self._configure_cli(environment)

    @with_prompt_template
    async def run(self, instruction: str, environment: BaseEnvironment,
                  context: AgentContext) -> None:
        await self._upload(environment, "instruction.md", instruction)
        result = None
        try:
            result = await self.exec_as_agent(environment, command=f"sh {OB / 'run.sh'} omnigent",
                                              env=self._run_env())
        finally:
            # Also on failure or timeout: an attempt with no answer is graded as one, not lost.
            await self._write_trajectory(environment, instruction, result)
