import json
import tempfile
import unittest
from pathlib import Path

from omar_bench import pricing, usage


def write_jsonl(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    return path


def assistant(message_id: str, model: str, **usage_fields) -> dict:
    return {"type": "assistant", "requestId": "req-" + message_id,
            "message": {"id": message_id, "model": model, "usage": usage_fields}}


class ClaudeTranscripts(unittest.TestCase):
    def setUp(self):
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_a_response_logged_on_several_lines_is_counted_once(self):
        # Claude Code writes one line per content block, each with the full usage.
        line = assistant("msg_1", "claude-opus-5-5", input_tokens=10, output_tokens=200,
                         cache_read_input_tokens=5000, cache_creation_input_tokens=300,
                         cache_creation={"ephemeral_5m_input_tokens": 100,
                                         "ephemeral_1h_input_tokens": 200})
        path = write_jsonl(self.dir / "projects/-app/s.jsonl",
                           [line, line, line, {"type": "user", "message": {"content": "hi"}}])
        self.assertEqual(usage.claude([path]), {"claude-opus-5-5": {
            "input": 10, "cache_read": 5000, "cache_write_5m": 100, "cache_write_1h": 200,
            "output": 200, "requests": 1}})

    def test_cache_writes_without_a_ttl_breakdown_bill_as_five_minute(self):
        path = write_jsonl(self.dir / "s.jsonl", [assistant(
            "msg_1", "m", input_tokens=1, output_tokens=1, cache_creation_input_tokens=40)])
        row = usage.claude([path])["m"]
        self.assertEqual((row["cache_write_5m"], row["cache_write_1h"]), (40, 0))
        # Only the one-hour part is itemised: the rest of what was written is five-minute.
        path = write_jsonl(self.dir / "t.jsonl", [assistant(
            "msg_2", "m", input_tokens=1, output_tokens=1, cache_creation_input_tokens=300,
            cache_creation={"ephemeral_1h_input_tokens": 200})])
        row = usage.claude([path])["m"]
        self.assertEqual((row["cache_write_5m"], row["cache_write_1h"]), (100, 200))

    def test_every_agent_of_a_team_is_summed_and_synthetic_messages_are_not(self):
        a = write_jsonl(self.dir / "projects/a/1.jsonl",
                        [assistant("msg_a", "m", input_tokens=1, output_tokens=10)])
        b = write_jsonl(self.dir / "projects/b/2.jsonl",
                        [assistant("msg_b", "m", input_tokens=2, output_tokens=20),
                         assistant("msg_x", "<synthetic>", input_tokens=9, output_tokens=9)])
        row = usage.claude([a, b])["m"]
        self.assertEqual((row["input"], row["output"], row["requests"]), (3, 30, 2))

    def test_a_torn_last_line_is_skipped(self):
        path = self.dir / "s.jsonl"
        path.write_text(json.dumps(assistant("msg_1", "m", input_tokens=1, output_tokens=1))
                        + '\n{"type": "assistant", "mess')
        self.assertEqual(usage.claude([path])["m"]["requests"], 1)

    def test_long_prompts_are_split_out_for_tiered_pricing(self):
        path = write_jsonl(self.dir / "s.jsonl", [
            assistant("short", "h", input_tokens=10, cache_read_input_tokens=90_000, output_tokens=1),
            assistant("long", "h", input_tokens=10, cache_read_input_tokens=100_000, output_tokens=2)])
        by_model = usage.claude([path], long_over={"h": 100_000})
        self.assertEqual(by_model["h"]["output"], 1)
        self.assertEqual(by_model["h" + usage.LONG]["output"], 2)
        self.assertEqual(usage.served_models(by_model), ["h"])


class CodexRollouts(unittest.TestCase):
    def test_the_last_running_total_is_the_session_and_cached_is_inside_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            def count(**total):
                return {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": total, "last_token_usage": total}}}
            path = write_jsonl(Path(tmp) / "sessions/2026/10/08/rollout-1.jsonl", [
                {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
                count(input_tokens=1000, cached_input_tokens=0, output_tokens=50),
                {"type": "event_msg", "payload": {"type": "token_count", "info": None}},
                count(input_tokens=5000, cached_input_tokens=4000, output_tokens=300,
                      reasoning_output_tokens=120)])
            self.assertEqual(usage.scan(Path(tmp)), {"gpt-5.6-sol": {
                "input": 1000, "cache_read": 4000, "cache_write_5m": 0, "cache_write_1h": 0,
                "output": 300, "requests": 2}})


class Pricing(unittest.TestCase):
    PRICES = {"as_of": "2026-10-06", "models": {
        "opus": {"input": 4.0, "output": 20.0, "cache_read": 0.2,
                 "cache_write_5m": 5.0, "cache_write_1h": 8.0},
        "haiku": {"aliases": ["haiku-dated"], "input": 0.1, "output": 0.5, "cache_read": 0.01,
                  "cache_write_5m": 0.125, "cache_write_1h": 0.2, "long_over": 100_000,
                  "long": {"input": 0.5, "output": 2.5, "cache_read": 0.05,
                           "cache_write_5m": 0.625, "cache_write_1h": 1.0}},
        "codex": {"input": 1.0, "output": 2.0, "cache_read": 0.1}}}

    def row(self, **fields):
        return {**dict.fromkeys(usage.FIELDS, 0), **fields}

    def test_each_token_class_is_billed_at_its_own_rate(self):
        usd, missing = pricing.cost({"opus": self.row(
            input=1_000_000, cache_read=1_000_000, cache_write_5m=1_000_000,
            cache_write_1h=1_000_000, output=1_000_000)}, self.PRICES)
        self.assertEqual((usd, missing), (4.0 + 0.2 + 5.0 + 8.0 + 20.0, []))

    def test_long_prompt_requests_use_the_long_rates_under_an_alias_too(self):
        by_model = {"haiku-dated": self.row(output=1_000_000),
                    "haiku-dated" + usage.LONG: self.row(output=1_000_000)}
        self.assertEqual(pricing.cost(by_model, self.PRICES), (0.5 + 2.5, []))
        self.assertEqual(pricing.long_over(self.PRICES),
                         {"haiku": 100_000, "haiku-dated": 100_000})

    def test_an_unpriced_model_makes_the_cost_unknown_not_smaller(self):
        usd, missing = pricing.cost({"opus": self.row(output=10), "glm": self.row(output=10)},
                                    self.PRICES)
        self.assertEqual((usd, missing), (None, ["glm"]))

    def test_tokens_with_no_rate_make_the_cost_unknown(self):
        self.assertEqual(pricing.cost({"codex": self.row(cache_write_5m=5)}, self.PRICES),
                         (None, ["codex"]))
        self.assertEqual(pricing.cost({"codex": self.row(input=1_000_000)}, self.PRICES),
                         (1.0, []))

    def test_a_cost_is_rounded_to_a_millionth_of_a_dollar(self):
        # Unrounded this is 0.12362320000000002.
        self.assertEqual(pricing.cost({"haiku": self.row(input=1_234_567, output=333)}, self.PRICES),
                         (0.123623, []))


if __name__ == "__main__":
    unittest.main()
