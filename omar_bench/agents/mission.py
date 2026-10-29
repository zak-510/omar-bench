"""Mission Control, driven as its operator would drive it, with nobody watching.

OMAR's web app, Mission Control, has an assistant that drafts a team for a task: the
operator reads the design and deploys it. While the team runs, the operator can ask for
a change, and apply the changed program the assistant proposes to the live run (a
mutation, OMAR #338). On a benchmark nobody is there, so this stands in, by one fixed
policy, through the HTTP API the web app itself uses:

- it sends the brief and deploys the first design the assistant proposes;
- when the team has written new results and a check-in is due, it shows them to the
  assistant, and applies the program the assistant proposes in answer, up to
  `max_mutations` times;
- a team that can make no more progress (run.sh's stall rule) ends the attempt, as on
  the topology arms: a mutation lands only at a tag boundary, which such a team never
  reaches.

It sees what an operator in Mission Control sees, never the task's tests. Every
message, design and mutation is kept beside the trial's logs, in mission-control/.
"""
from __future__ import annotations

import asyncio
import json
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

# A run in any other status has ended.
ACTIVE = {"starting", "running", "stopping", "pausing", "paused"}
NUDGE = ("Nobody can answer questions here. Decide what you would assume, say so, and "
         "propose the complete program with omar_propose_design.")
VALUE_CHARS = 1500     # of any one port's value, in a check-in
CHECKIN_CHARS = 12000  # of all of them


class Mission:
    def __init__(self, shell: Callable[[str], Awaitable[str]],
                 upload: Callable[[str, str], Awaitable[str]], logs: Path, *, url: str,
                 brief: str, max_mutations: int, turn_timeout_sec: int, stalled_command: str,
                 poll_sec: float = 30,
                 checkin_sec: float = 900, reply_timeout_sec: float = 1800,
                 design_timeout_sec: float = 2700, max_nudges: int = 2,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        """`shell` runs a command in the task container and returns its output; `upload`
        puts text in a file there and returns its path; `stalled_command` prints why the
        team can make no more progress, or nothing; `logs` is the trial's agent log
        directory on this machine."""
        self._shell, self._upload, self._url = shell, upload, url.rstrip("/")
        self._brief, self._max_mutations = brief, max_mutations
        self._turn_timeout_sec, self._stalled_command = turn_timeout_sec, stalled_command
        self._poll, self._checkin, self._reply_timeout = poll_sec, checkin_sec, reply_timeout_sec
        self._design_timeout, self._max_nudges = design_timeout_sec, max_nudges
        self._clock, self._sleep = clock, sleep
        self._logs = Path(logs)
        self._dir = self._logs / "mission-control"
        self._dir.mkdir(parents=True, exist_ok=True)
        self.mutations = 0

    # ------------------------------------------------------------ record --------

    def _note(self, text: str) -> None:
        with open(self._dir / "log.txt", "a") as log:
            log.write(f"{datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ} {text}\n")

    def _keep(self, name: str, design: dict) -> None:
        """A design as proposed: its program, and the files it imports beside it."""
        (self._dir / f"{name}.omar").write_text(design.get("program", ""))
        for rel, text in (design.get("files") or {}).items():
            path = self._dir / name / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        if design.get("inputs"):
            (self._dir / f"{name}.inputs.json").write_text(json.dumps(design["inputs"], indent=2))

    def _fail(self, why: str) -> str:
        self._note(f"no team ran: {why}")
        (self._logs / "omar-mission-failed.txt").write_text(
            f"omar-bench ended this attempt: Mission Control {why}\n")
        return "failed"

    # -------------------------------------------------------------- http --------

    async def _call(self, method: str, path: str, body=None) -> tuple[int, object]:
        """One request to the session's server from inside the container. Status 0 when
        nothing answered."""
        command = f"curl -sS -X {method} -w '\\n%{{http_code}}'"
        if body is not None:
            request = await self._upload("request.json", json.dumps(body))
            command += f" -H 'content-type: application/json' --data-binary @{shlex.quote(request)}"
        out = await self._shell(f"{command} {shlex.quote(self._url + path)} || true")
        text, _, code = out.rstrip("\n").rpartition("\n")
        status = int(code) if code.strip().isdigit() else 0
        try:
            return status, json.loads(text) if text.strip() else None
        except ValueError:
            return status, text

    async def _chat(self) -> dict:
        status, chat = await self._call("GET", "/v1/chat")
        return chat if status == 200 and isinstance(chat, dict) else {}

    async def _say(self, text: str, patience: float = 300) -> int | None:
        """Sends the operator's message; its sequence number, or None if the assistant
        never took it (still answering the last one, or not running)."""
        deadline = self._clock() + patience
        while True:
            status, sent = await self._call("POST", "/v1/chat", {"text": text})
            if 200 <= status < 300 and isinstance(sent, dict):
                self._note(f"operator: {text[:200]!r}")
                return int(sent.get("sequence") or 0)
            if self._clock() >= deadline:
                self._note(f"the assistant did not take a message ({status}: {sent})")
                return None
            await self._sleep(self._poll)

    async def _answer(self, after: int, timeout: float, run_id: str | None = None):
        """The assistant's messages after sequence `after`, until it hands the turn back:
        (its design or None, the text it ended on). Gives up after `timeout`, or as soon
        as the run (when there is one) has ended."""
        deadline = self._clock() + timeout
        seen = after
        while self._clock() < deadline:
            for message in (await self._chat()).get("messages", []):
                if message.get("sequence", 0) <= seen or message.get("role") != "assistant":
                    continue
                seen = message["sequence"]
                self._note(f"assistant{' (progress)' if message.get('progress') else ''}: "
                           f"{message.get('text', '')[:500]!r}")
                if message.get("design"):
                    return message["design"], message.get("text", "")
                if not message.get("progress"):
                    return None, message.get("text", "")
            if run_id is not None and (await self._run(run_id)).get("status") not in ACTIVE:
                break
            await self._sleep(self._poll)
        return None, None

    async def _run(self, run_id: str) -> dict:
        status, record = await self._call("GET", f"/v1/runs/{run_id}")
        return record if status == 200 and isinstance(record, dict) else {}

    # ------------------------------------------------------------ design --------

    async def _design(self, text: str):
        """Sends `text`, then waits for a design, nudging an assistant that asks instead."""
        deadline = self._clock() + self._design_timeout
        nudges = 0
        while True:
            after = await self._say(text)
            if after is None:
                return None
            design, said = await self._answer(after, max(0.0, deadline - self._clock()))
            if design is not None:
                return design
            if said is None or nudges >= self._max_nudges:
                return None
            nudges += 1
            text = NUDGE

    async def _deploy(self, design: dict) -> tuple[str | None, str]:
        status, record = await self._call("POST", "/v1/runs", {
            "program": design.get("program", ""), "files": design.get("files") or {},
            "inputs": design.get("inputs") or {},
            # The arm's turn limit, as on the topology arms; Mission Control's own is 5 min.
            "timeout_seconds": self._turn_timeout_sec,
            "fast": True, "replace": True, "keep_workspaces": True,
            # Checkpoints serve pause and rollback; a mutation pauses the run itself.
            "checkpoint_period": "off",
        })
        if 200 <= status < 300 and isinstance(record, dict) and record.get("run_id"):
            return record["run_id"], ""
        error = record.get("error") if isinstance(record, dict) else record
        return None, f"{status}: {error}"

    # ------------------------------------------------------------- watch --------

    async def _report(self, run_id: str, seen: set) -> tuple[str, str] | None:
        """What the team has written since the last check-in, for the assistant; None if
        nothing. `seen` collects what has been shown."""
        status, snapshot = await self._call("GET", f"/v1/runs/{run_id}/snapshot")
        if status != 200 or not isinstance(snapshot, dict):
            return None
        written = []
        for port in snapshot.get("ports", []):
            key = (port.get("id"), json.dumps(port.get("last_tag"), sort_keys=True))
            if port.get("value") is None or port.get("last_tag") is None or key in seen:
                continue
            seen.add(key)
            value = port["value"] if isinstance(port["value"], str) else json.dumps(port["value"])
            if len(value) > VALUE_CHARS:
                value = value[:VALUE_CHARS] + f" [... {len(value) - VALUE_CHARS} more characters]"
            written.append(f"- {port.get('instance') or ''}{'.' if port.get('instance') else ''}"
                           f"{port.get('name')} ({port.get('type')}): {value}")
        if not written:
            return None
        running = [f"{r.get('instance') or ''}{'.' if r.get('instance') else ''}{r.get('name')}"
                   for r in snapshot.get("reactions", []) if r.get("status") == "running"]
        report = "\n".join(written)
        if len(report) > CHECKIN_CHARS:
            report = report[:CHECKIN_CHARS] + "\n[... cut]"
        return report, ", ".join(running) or "none"

    async def _check_in(self, run_id: str, report, started: float) -> None:
        written, running = report
        minutes = int((self._clock() - started) / 60)
        after = await self._say(
            f"Check-in, {minutes} minutes into the run. Nobody can answer questions.\n\n"
            f"Since the last check-in the team wrote:\n{written}\n\n"
            f"Steps still running: {running}.\n\n"
            "If this shows the design will not complete the task, propose the changed "
            "program now, keeping main's name: it is applied to the running team, and every "
            "agent restarts with a fresh conversation. Otherwise reply with omar_reply that "
            "no change is needed.", patience=0)
        if after is None:
            return
        design, _ = await self._answer(after, self._reply_timeout, run_id)
        if design is None:
            return
        n = self.mutations + 1
        self._keep(f"mutation-{n}", design)
        status, result = await self._call("POST", f"/v1/runs/{run_id}/mutate", {
            "program": design.get("program", ""), "files": design.get("files") or {}})
        if 200 <= status < 300:
            self.mutations = n
            self._note(f"mutation {n} applied for: {json.dumps(result)[:500]}")
        else:
            self._note(f"mutation {n} refused ({status}): {json.dumps(result)[:500]}")

    async def _watch(self, run_id: str) -> str:
        started = last = self._clock()
        seen: set = set()
        while True:
            record = await self._run(run_id)
            status = record.get("status")
            if record and status not in ACTIVE:
                self._end(record)
                return status
            if (await self._shell(self._stalled_command)).strip():
                self._note("the team can make no more progress: stopping the run")
                await self._call("POST", f"/v1/runs/{run_id}/stop", {})
                self._end(await self._run(run_id))
                return "stalled"
            pending = (record.get("mutation") or {}).get("status") == "pending"
            if status == "paused" and not pending:
                # Only a mutation pauses this run, and one that is done resumes it.
                await self._call("POST", f"/v1/runs/{run_id}/resume", {})
            elif (status == "running" and not pending and self.mutations < self._max_mutations
                  and self._clock() - last >= self._checkin):
                report = await self._report(run_id, seen)
                if report is not None:
                    last = self._clock()
                    await self._check_in(run_id, report, started)
                    continue
            await self._sleep(self._poll)

    def _end(self, record: dict) -> None:
        """The run as it ended, in the form `omar run` prints, where the adapter reads it."""
        (self._dir / "run.json").write_text(json.dumps(record, indent=2))
        lines = [f"Run {record.get('run_id')} {record.get('status')}"
                 + (f": {record['error']}" if record.get("error") else "")]
        lines += [f"Output {port} = {json.dumps(value)}"
                  for port, value in (record.get("outputs") or {}).items()]
        (self._logs / "omar-run.txt").write_text("\n".join(lines) + "\n")
        self._note(f"run ended {record.get('status')} after {self.mutations} mutation(s)")

    # --------------------------------------------------------------- run --------

    async def run(self) -> str:
        """Designs, deploys and watches one team; how the attempt ended."""
        try:
            deadline = self._clock() + 120
            while (await self._call("GET", "/health"))[0] != 200:
                if self._clock() >= deadline:
                    return self._fail("never answered on its address")
                await self._sleep(2)
            design, text = None, self._brief
            for tries in range(3):
                design = await self._design(text)
                if design is None:
                    return self._fail("proposed no design")
                self._keep(f"design-{tries + 1}", design)
                run_id, error = await self._deploy(design)
                if run_id is not None:
                    self._note(f"deployed design-{tries + 1} as run {run_id}")
                    return await self._watch(run_id)
                self._note(f"design-{tries + 1} would not start: {error}")
                text = (f"That program would not start: {error}\nFix it and propose the "
                        "complete program again.")
            return self._fail("proposed no design that would start")
        finally:
            try:
                (self._dir / "chat.json").write_text(json.dumps(await self._chat(), indent=2))
            except Exception as error:  # the attempt's end matters more than this record
                self._note(f"could not keep the chat: {error!r}")
