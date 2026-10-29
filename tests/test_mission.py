"""The Mission Control driver, against a stand-in for the session's HTTP API.

No container and no model: the stand-in answers the curl commands the driver would run in
the container, plays the assistant from a script, and keeps time on a fake clock.
"""
import asyncio
import json
import shlex
import tempfile
import unittest
from pathlib import Path

from omar_bench.agents.mission import IN_PLACE, NUDGE, TEST_CHANGE, Mission

STALLED = "sh run.sh stalled"
DESIGN = {"program": "main Bench { t = Team() }", "files": {"schemas/x.json": "{}"},
          "inputs": {"t.statement": "/w/instruction.md"}}


def said(text, design=None, progress=False):
    return {"role": "assistant", "text": text, "progress": progress, "design": design}


class Session:
    """Mission Control's server as the driver sees it, and the assistant behind it."""

    def __init__(self, *answers, runs=(), snapshots=(), deploy_errors=(), mutate_errors=(),
                 stall_at=None, edits_in_place=False):
        self.answers = list(answers)       # one list of messages per operator message
        self.runs = list(runs)             # the run's record on successive reads; the last stays
        self.snapshots = list(snapshots)   # its snapshot on successive reads; the last stays
        self.deploy_errors = list(deploy_errors)
        self.mutate_errors = list(mutate_errors)
        self.stall_at = stall_at           # the clock time from which the team counts as stalled
        # An assistant that edits the run's program file before it proposes the result.
        self.edits_in_place = edits_in_place
        self.staged = None                 # the run's program file
        self.restored = 0
        self.messages, self.pending, self.busy = [], [], False
        self.calls, self.files = [], {}
        self.now = 0.0

    # What the adapter gives the driver.
    async def shell(self, command):
        if command == STALLED:
            stalled = self.stall_at is not None and self.now >= self.stall_at
            return "omar-bench ended this attempt: every agent's turn was over\n" if stalled else ""
        if command.startswith("set -- "):  # the program file put back
            assert "/serve/r1/*.omar" in command, command
            self.staged = self.files[shlex.split(command.split("&& cp ")[1])[0]]
            self.restored += 1
            return "restored\n"
        args = shlex.split(command.removesuffix(" || true"))
        method = args[args.index("-X") + 1]
        body = None
        if "--data-binary" in args:
            body = json.loads(self.files[args[args.index("--data-binary") + 1].removeprefix("@")])
        path = args[-1].removeprefix("http://127.0.0.1:4100")
        self.calls.append((method, path, body, self.now))
        status, answer = self.handle(method, path, body)
        return f"{json.dumps(answer)}\n{status}"

    async def upload(self, name, text):
        self.files[f"/ob/mission-{name}"] = text
        return f"/ob/mission-{name}"

    async def sleep(self, seconds):
        self.now += seconds

    def clock(self):
        return self.now

    def handle(self, method, path, body):
        if path == "/health":
            return 200, {"status": "ok"}
        if (method, path) == ("POST", "/v1/chat"):
            if self.busy:
                return 409, {"error": "The assistant is still replying."}
            message = self.append({"role": "operator", "text": body["text"], "progress": False,
                                   "design": None})
            self.busy = True
            self.pending = self.answers.pop(0) if self.answers else []
            return 202, message
        if (method, path) == ("GET", "/v1/chat"):
            for message in self.pending:
                self.append(dict(message))
                if message["design"] and self.edits_in_place and self.staged is not None:
                    self.staged = message["design"]["program"]
                if not message["progress"] or message["design"]:
                    self.busy = False
            self.pending = []
            return 200, {"id": "c1", "messages": self.messages}
        if (method, path) == ("POST", "/v1/runs"):
            if self.deploy_errors:
                return 400, {"error": self.deploy_errors.pop(0)}
            self.staged = body["program"]
            return 201, {"run_id": "r1", "status": "running"}
        if path == "/v1/runs/r1/program":
            return 200, {"filename": "program.omar", "program": self.staged}
        if path == "/v1/runs/r1/mutate":
            # Planned from the run's program file, as the runtime plans it.
            if body["program"] == self.staged:
                return 200, {"changes": []}
            if self.mutate_errors:
                return 400, {"error": self.mutate_errors.pop(0)}
            return 202, {"run_id": "r1", "status": "running", "mutation": {"status": "pending"}}
        if path == "/v1/runs/r1":
            record = self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]
            return 200, {"run_id": "r1", **record}
        if path == "/v1/runs/r1/snapshot":
            return 200, self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]
        if method == "POST" and path.startswith("/v1/runs/r1/"):
            return 202, {"status": "pending"}
        return 404, {"error": "unknown"}

    def append(self, message):
        message["sequence"] = len(self.messages) + 1
        self.messages.append(message)
        return message

    def sent(self):
        return [m["text"] for m in self.messages if m["role"] == "operator"]

    def posted(self, path):
        return [body for method, p, body, _ in self.calls if method == "POST" and p == path]

    def when(self, path):
        return [now for method, p, _, now in self.calls if method == "POST" and p == path]


def port(name, value, at):
    return {"id": f"t.{name}", "name": name, "instance": "t", "type": "string", "value": value,
            "last_tag": {"timestamp": at, "microstep": 0}}


RUNNING, DONE = {"status": "running"}, {"status": "completed", "outputs": {"t.answer": "done"}}
QUIET = {"ports": [port("plan", None, None)], "reactions": []}
WROTE = {"ports": [port("plan", "Split the parser out first.", 7)],
         "reactions": [{"name": "build", "instance": "t", "status": "running"}]}
CHANGED = {"program": "main Bench { t = Team(); r = Reviewer() }", "files": {}}
PENDING = {"status": "running", "mutation": {"status": "pending"}}
APPLIED = {"status": "running", "mutation": {"status": "applied", "checkpoint": "m1",
                                             "changes": ["declare_instance r = Reviewer"]}}


class MissionControl(unittest.TestCase):
    def drive(self, session, **kwargs):
        self.logs = Path(self.enterContext(tempfile.TemporaryDirectory()))
        kwargs = {"max_mutations": 3, "checkin_sec": 900, **kwargs}
        mission = Mission(session.shell, session.upload, self.logs, url="http://127.0.0.1:4100",
                          brief="Design a team for the task.", turn_timeout_sec=28800,
                          stalled_command=STALLED, poll_sec=30, clock=session.clock,
                          sleep=session.sleep, **kwargs)
        self.mission = mission
        return asyncio.run(mission.run())

    def test_the_first_design_is_deployed_with_the_arms_limits_and_kept(self):
        session = Session([said("Reading the task.", progress=True), said("Here it is.", DESIGN)],
                          runs=[RUNNING, RUNNING, DONE], snapshots=[QUIET])
        self.assertEqual(self.drive(session), "completed")

        self.assertEqual(session.sent(), ["Design a team for the task."])
        (deployed,) = session.posted("/v1/runs")
        self.assertEqual({k: deployed[k] for k in ("program", "files", "inputs")}, DESIGN)
        # The arm's turn limit, not Mission Control's five minutes; workspaces kept for the
        # record; no checkpoints but the ones a mutation takes.
        self.assertEqual(deployed["timeout_seconds"], 28800)
        self.assertTrue(deployed["fast"] and deployed["keep_workspaces"])
        self.assertEqual(deployed["checkpoint_period"], "off")
        kept = self.logs / "mission-control"
        self.assertEqual((kept / "design-1.omar").read_text(), DESIGN["program"])
        self.assertEqual((kept / "design-1/schemas/x.json").read_text(), "{}")
        self.assertEqual(len(json.loads((kept / "chat.json").read_text())["messages"]), 3)
        # Where the adapter reads the team's answer, as `omar run` would have printed it.
        self.assertIn('Output t.answer = "done"', (self.logs / "omar-run.txt").read_text())
        self.assertFalse((self.logs / "omar-mission-failed.txt").exists())

    def test_an_assistant_that_asks_is_told_nobody_answers_then_given_up_on(self):
        question = [said("Which language should it use?")]
        session = Session(question, question, question, runs=[RUNNING], snapshots=[QUIET])
        self.assertEqual(self.drive(session, max_nudges=2), "failed")
        self.assertEqual(session.sent()[1:], [NUDGE, NUDGE])
        self.assertEqual(session.posted("/v1/runs"), [])
        self.assertIn("proposed no design", (self.logs / "omar-mission-failed.txt").read_text())

    def test_a_question_then_a_design_runs_the_design(self):
        session = Session([said("Which language?")], [said("Assuming Python.", DESIGN)],
                          runs=[DONE], snapshots=[QUIET])
        self.assertEqual(self.drive(session), "completed")
        self.assertEqual(session.sent()[1:], [NUDGE])

    def test_a_design_that_will_not_start_goes_back_with_the_error(self):
        fixed = {**DESIGN, "program": "main Bench { t = Fixed() }"}
        session = Session([said("v1", DESIGN)], [said("v2", fixed)], runs=[DONE],
                          snapshots=[QUIET], deploy_errors=["input 't.x' is not declared"])
        self.assertEqual(self.drive(session), "completed")
        self.assertIn("input 't.x' is not declared", session.sent()[1])
        self.assertEqual([b["program"] for b in session.posted("/v1/runs")],
                         [DESIGN["program"], fixed["program"]])
        self.assertTrue((self.logs / "mission-control/design-2.omar").exists())

    def test_new_results_go_to_the_assistant_and_its_change_is_applied(self):
        session = Session([said("ok", DESIGN)], [said("Adding a reviewer.", CHANGED)],
                          runs=[RUNNING] * 40 + [PENDING] * 5 + [APPLIED, DONE], snapshots=[WROTE])
        self.assertEqual(self.drive(session), "completed")

        brief, check_in = session.sent()
        self.assertIn("t.plan (string): Split the parser out first.", check_in)
        self.assertIn("Steps still running: t.build.", check_in)
        self.assertIn(IN_PLACE, check_in)
        (mutated,) = session.posted("/v1/runs/r1/mutate")
        self.assertEqual(mutated["program"], CHANGED["program"])
        self.assertEqual((self.mission.mutations, self.mission.applied), (1, 1))
        self.assertEqual((self.logs / "mission-control/mutation-1.omar").read_text(),
                         CHANGED["program"])
        self.assertIn("mutation 1 applied: checkpoint m1, 1 change(s)",
                      (self.logs / "mission-control/log.txt").read_text())
        # Not before a check-in was due.
        deployed_at, = session.when("/v1/runs")
        self.assertGreaterEqual(session.when("/v1/chat")[1] - deployed_at, 900)

    def test_a_program_edited_in_place_is_put_back_before_the_change_is_planned(self):
        session = Session([said("ok", DESIGN)], [said("Adding a reviewer.", CHANGED)],
                          runs=[RUNNING] * 40 + [PENDING, APPLIED, DONE], snapshots=[WROTE],
                          edits_in_place=True)
        self.assertEqual(self.drive(session), "completed")
        self.assertEqual(session.restored, 1)
        self.assertEqual(len(session.posted("/v1/runs/r1/mutate")), 1)
        self.assertEqual(self.mission.applied, 1)

    def test_a_proposal_that_changes_nothing_is_not_a_mutation(self):
        session = Session([said("ok", DESIGN)], [said("Same again.", DESIGN)],
                          runs=[RUNNING] * 40 + [DONE], snapshots=[WROTE])
        self.assertEqual(self.drive(session), "completed")
        self.assertEqual((self.mission.mutations, self.mission.applied), (0, 0))
        self.assertIn("changes nothing", (self.logs / "mission-control/log.txt").read_text())

    def test_a_refused_change_goes_back_once_with_the_error(self):
        fixed = {**CHANGED, "program": "main Bench { t = Team(); r = Fixed() }"}
        session = Session([said("ok", DESIGN)], [said("v1", CHANGED)], [said("v2", fixed)],
                          runs=[RUNNING] * 40 + [PENDING, APPLIED, DONE], snapshots=[WROTE],
                          mutate_errors=["the new program renames main"])
        self.assertEqual(self.drive(session), "completed")
        self.assertIn("the new program renames main", session.sent()[2])
        self.assertEqual([b["program"] for b in session.posted("/v1/runs/r1/mutate")],
                         [CHANGED["program"], fixed["program"]])
        self.assertEqual((self.mission.mutations, self.mission.applied), (1, 1))

    def test_a_change_the_run_could_not_apply_is_recorded_as_failed(self):
        failed = {"status": "paused", "mutation": {"status": "failed", "error": "plan check"}}
        session = Session([said("ok", DESIGN)], [said("Adding a reviewer.", CHANGED)],
                          runs=[RUNNING] * 40 + [PENDING, failed, RUNNING, DONE], snapshots=[WROTE])
        self.assertEqual(self.drive(session), "completed")
        self.assertEqual((self.mission.mutations, self.mission.applied), (1, 0))
        self.assertIn("mutation 1 failed: plan check",
                      (self.logs / "mission-control/log.txt").read_text())
        self.assertEqual(session.posted("/v1/runs/r1/resume"), [{}])

    def test_a_shakedown_asks_for_a_change_at_the_first_check_in(self):
        session = Session([said("ok", DESIGN)], [said("Adding a check.", CHANGED)],
                          runs=[RUNNING] * 5 + [PENDING, APPLIED] + [RUNNING] * 100 + [DONE],
                          snapshots=[QUIET])
        self.assertEqual(self.drive(session, checkin_sec=60, test_mutation=True), "completed")
        brief, check_in = session.sent()  # once only, and not again without results
        self.assertIn("the team wrote:\nnothing yet", check_in)
        self.assertIn(TEST_CHANGE, check_in)
        self.assertEqual(self.mission.applied, 1)

    def test_without_new_results_there_is_no_check_in(self):
        session = Session([said("ok", DESIGN)], runs=[RUNNING] * 100 + [DONE], snapshots=[QUIET])
        self.assertEqual(self.drive(session), "completed")
        self.assertEqual(len(session.sent()), 1)

    def test_results_already_shown_are_not_shown_again(self):
        wrote = {"ports": [port("plan", "p", 7)], "reactions": []}
        session = Session([said("ok", DESIGN)], [said("No change needed.")],
                          runs=[RUNNING] * 200 + [DONE], snapshots=[wrote])
        self.assertEqual(self.drive(session), "completed")
        self.assertEqual(len(session.sent()), 2)
        self.assertEqual(session.posted("/v1/runs/r1/mutate"), [])

    def test_mutations_stop_at_the_cap(self):
        changed = {"program": "main Bench { t = Other() }", "files": {}}
        snapshots = [{"ports": [port("plan", f"step {n}", n)], "reactions": []} for n in range(1, 50)]
        session = Session([said("ok", DESIGN)], [said("change", changed)], [said("again", changed)],
                          runs=[RUNNING] * 40 + [PENDING, APPLIED] + [RUNNING] * 300 + [DONE],
                          snapshots=snapshots)
        self.assertEqual(self.drive(session, max_mutations=1), "completed")
        self.assertEqual(len(session.posted("/v1/runs/r1/mutate")), 1)
        self.assertEqual(len(session.sent()), 2)

    def test_a_team_that_can_make_no_progress_is_stopped(self):
        session = Session([said("ok", DESIGN)], runs=[RUNNING] * 100 + [DONE], snapshots=[QUIET],
                          stall_at=600)
        self.assertEqual(self.drive(session), "stalled")
        self.assertEqual(session.posted("/v1/runs/r1/stop"), [{}])

    def test_a_run_left_paused_after_a_mutation_is_resumed(self):
        session = Session([said("ok", DESIGN)], runs=[{"status": "paused"}, RUNNING, DONE],
                          snapshots=[QUIET])
        self.assertEqual(self.drive(session), "completed")
        self.assertEqual(session.posted("/v1/runs/r1/resume"), [{}])


if __name__ == "__main__":
    unittest.main()
