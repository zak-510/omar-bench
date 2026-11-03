"""Omnigent's Polly as a treatment: its bundle, and the watcher that follows it to its end.

Standard library only: the watcher is driven against a stand-in for Omnigent's server.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from omar_bench import polly
from omar_bench.config import ROOT

WATCH = ROOT / "omar_bench/agents/container/omnigent_watch.py"


class Bundle(unittest.TestCase):
    def test_one_model_does_everything_and_the_rest_is_as_shipped(self):
        files = polly.render("codex", "gpt-6.1-sol", "max")
        config = files["config.yaml"]
        shipped = (polly.POLLY / "config.yaml").read_text()
        # The lead on the arm's CLI and model; its effort as far as its harness goes.
        self.assertIn("executor:\n  type: omnigent\n  context_window: 1000000\n"
                      "  model: gpt-6.1-sol\n  reasoning_effort: xhigh\n"
                      "  config:\n    harness: codex\n", config)
        self.assertNotIn("smart_routing_harness", config)
        # The one worker whose CLI is installed, pinned to the same model at the suite's effort.
        self.assertIn("  agents:\n    - codex\n\n", config)
        self.assertEqual(sorted(p for p in files if p.startswith("agents/")),
                         ["agents/codex/config.yaml"])
        self.assertIn("executor:\n  type: omnigent\n  model: gpt-6.1-sol\n  reasoning_effort: max\n",
                      files["agents/codex/config.yaml"])
        # Another model refused, in Polly's own guardrails, after its own policies.
        self.assertIn("single_model:", config)
        self.assertLess(config.index("headless_subagent_purpose_guard"), config.index("single_model"))
        expression = json.loads(config.split("expression: ", 1)[1].splitlines()[0])
        self.assertIn('event.data.arguments.args.model != "gpt-6.1-sol"', expression)
        self.assertIn('"result": "DENY"', expression)
        # Everything else is Polly's own text: its prompt and its skills untouched.
        prompt = shipped[shipped.index("prompt: |"):shipped.index("\ntools:")]
        self.assertIn(prompt, config)
        self.assertEqual(files["skills/cross-review/SKILL.md"],
                         (polly.POLLY / "skills/cross-review/SKILL.md").read_text())

    def test_an_effort_its_lead_can_take_is_given_as_it_is(self):
        config = polly.render("codex", "gpt-6.1-sol", "high")["config.yaml"]
        self.assertIn("  reasoning_effort: high\n  config:\n    harness: codex\n", config)
        claude = polly.render("claude", "claude-sonnet-5-5", "max")
        self.assertIn("  model: claude-sonnet-5-5\n  reasoning_effort: max\n"
                      "  config:\n    harness: claude-sdk\n", claude["config.yaml"])
        self.assertIn("agents/claude_code/config.yaml", claude)

    def test_a_changed_polly_is_refused_rather_than_half_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "polly"
            subprocess.run(["cp", "-R", str(polly.POLLY), str(copy)], check=True)
            config = copy / "config.yaml"
            config.write_text(config.read_text().replace("    - hermes\n", ""))
            old, polly.POLLY = polly.POLLY, copy
            try:
                with self.assertRaisesRegex(ValueError, "roster"):
                    polly.render("codex", "gpt-6.1-sol", "max")
            finally:
                polly.POLLY = old


class Server:
    """Omnigent's server, standing in: sessions whose status changes as they are read.

    A session's "status" is a list; each read of it takes the next, the last one stays.
    """

    def __init__(self, sessions: dict, children: dict, items: dict):
        self.sessions, self.children, self.items = sessions, children, items
        self.reads: dict[str, int] = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                parts = urlparse(self.path).path.strip("/").split("/")[2:]  # after v1/sessions
                if not parts:
                    body = {"data": [server.view(s) for s in server.sessions
                                     if not server.sessions[s].get("parent_session_id")]}
                elif len(parts) == 1:
                    body = server.view(parts[0])
                elif parts[1] == "child_sessions":
                    body = {"data": [{"id": c} for c in server.children.get(parts[0], [])]}
                else:
                    body = {"data": server.items.get(parts[0], [])}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_address[1]}"

    def view(self, sid: str) -> dict:
        s = dict(self.sessions[sid])
        statuses = s.pop("statuses")
        n = self.reads.get(sid, 0)
        self.reads[sid] = n + 1
        s["status"] = statuses[min(n, len(statuses) - 1)]
        return s


def session(sid, statuses, parent=None, **fields):
    return {"id": sid, "agent_name": "polly", "parent_session_id": parent, "statuses": statuses,
            "updated_at": time.time(), "pending_inputs": [], "pending_elicitations": [], **fields}


class Watcher(unittest.TestCase):
    def watch(self, server, **env):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        logs = tmp / "logs"
        logs.mkdir()
        run = subprocess.run(
            [sys.executable, str(WATCH), server.url, str(logs)], capture_output=True, text=True,
            timeout=60, env={"PATH": os.environ["PATH"], "HOME": str(tmp), "OB_WATCH_SECS": "1",
                             "OB_SWARM_SETTLE_SECS": "2", **env})
        self.addCleanup(server.http.shutdown)
        return run, logs

    LEAD_SAID = [{"type": "message", "role": "assistant", "content": [{"text": "Fixed in place."}]},
                 {"type": "message", "role": "user", "content": [{"text": "[System: done]"}]}]
    USAGE = {"gpt-6.1-sol": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 5}}

    def test_it_is_done_once_the_lead_and_every_sub_agent_have_settled(self):
        # The lead's turn ends while its worker still runs: that is not the end.
        server = Server({"lead": session("lead", ["running", "idle"], usage_by_model=self.USAGE),
                         "w1": session("w1", ["running"] * 4 + ["idle"], parent="lead",
                                       sub_agent_name="codex")},
                        {"lead": ["w1"]}, {"lead": self.LEAD_SAID})
        run, logs = self.watch(server)

        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertGreaterEqual(server.reads["w1"], 5)
        self.assertIn('Output t.answer = "Fixed in place."', run.stdout)
        # What the attempt cost is the lead's: Omnigent reports it for the whole subtree.
        self.assertEqual(json.loads((logs / "omnigent-usage.json").read_text()), self.USAGE)
        records = json.loads((logs / "omnigent-sessions.json").read_text())
        self.assertEqual([r["session"]["id"] for r in records], ["lead", "w1"])
        self.assertEqual(records[0]["items"], self.LEAD_SAID)
        self.assertFalse((logs / "omar-stall.txt").exists())

    def test_a_session_parked_where_nobody_answers_ends_the_attempt(self):
        lead = session("lead", ["waiting"], pending_elicitations=[{"id": "ask-1"}])
        lead["updated_at"] = 0
        run, logs = self.watch(Server({"lead": lead}, {}, {}), OB_DEAD_TURN_MINS="0")

        self.assertEqual(run.returncode, 70, run.stdout + run.stderr)
        stall = (logs / "omar-stall.txt").read_text()
        # Said as stalled() says it, so the queue reads it as a step left open.
        self.assertIn("every agent's turn was over", stall)
        self.assertIn("1 approval(s) nobody will give", stall)
        self.assertIn(stall, run.stdout)

    def test_a_lead_whose_turn_failed_is_named_as_the_cli_gave_it(self):
        error = "Selected model is at capacity. Please try a different model."
        run, logs = self.watch(Server({"lead": session("lead", ["idle"], last_task_error=error)},
                                      {}, {}))

        self.assertEqual(run.returncode, 70, run.stdout + run.stderr)
        self.assertIn(error, (logs / "omar-stall.txt").read_text())
        self.assertIn(error, run.stdout)

    def test_no_session_at_all_is_a_setup_failure(self):
        run, logs = self.watch(Server({}, {}, {}), OB_OMNIGENT_START_SECS="2")
        self.assertEqual(run.returncode, 71, run.stdout + run.stderr)
        self.assertIn("no Polly session", run.stdout)


if __name__ == "__main__":
    unittest.main()
