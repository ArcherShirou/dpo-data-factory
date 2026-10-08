import json
import contextlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import audit
from dpo_factory import Pipeline, run


ROOT = Path(__file__).resolve().parent


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.pipeline = Pipeline(json.loads((ROOT / "config.mock.json").read_text()), 42)

    def test_wrong_top_answer_is_corrected(self):
        task = json.loads((ROOT / "tasks.example.jsonl").read_text().splitlines()[0])
        pairs = self.pipeline.routes_1_2(task)
        self.assertEqual({p["meta"]["route"] for p in pairs}, {"route1", "route2"})
        self.assertTrue(pairs[0]["meta"]["corrected"])
        self.assertIn("北京", pairs[0]["chosen"])
        self.assertEqual(pairs[0]["rejected"], "上海。")

    def test_untrusted_objective_task_is_skipped(self):
        task = {"id": "bad", "prompt": "首都？", "kind": "objective",
                "reference": "北京", "reference_source": "model"}
        self.assertEqual(self.pipeline.routes_1_2(task), [])

    def test_open_pair_needs_acceptable_chosen(self):
        task = {"id": "open", "kind": "open", "prompt": "请回答问题"}
        with patch.object(self.pipeline, "compare", return_value=("差答案", ["差答案", "差答案"])):
            with patch.object(self.pipeline, "verdict", return_value={"passed": False, "reason": "off topic"}):
                self.assertIsNone(self.pipeline.decide(
                    task, "差答案", "更差答案", "route1", "gpt6", "gpt6"))
        self.assertEqual(self.pipeline.stats["skip_low_quality_chosen"], 1)

    def test_rewrite_rejected_by_claude_is_skipped(self):
        task = {"id": "mail", "kind": "open", "prompt": "写一封简短邮件，婉拒本周的会议邀请。"}
        original_compare = self.pipeline.compare

        def compare(judge, item, a, b):
            return (a, [a, a]) if judge == "claude" else original_compare(judge, item, a, b)

        with patch.object(self.pipeline, "compare", side_effect=compare):
            pairs = self.pipeline.routes_1_2(task)
        self.assertEqual([p["meta"]["route"] for p in pairs], ["route1"])

    def test_factual_check_overrides_bad_rewrite_vote(self):
        task = json.loads((ROOT / "tasks.example.jsonl").read_text().splitlines()[0])
        original_compare = self.pipeline.compare

        def compare(judge, item, a, b):
            return (a, [a, a]) if judge == "claude" else original_compare(judge, item, a, b)

        with patch.object(self.pipeline, "compare", side_effect=compare):
            pairs = self.pipeline.routes_1_2(task)
        rewrite = next(p for p in pairs if p["meta"]["route"] == "route2")
        self.assertTrue(rewrite["meta"]["trace"]["factual_override"])
        self.assertIn("北京", rewrite["chosen"])
        self.assertEqual(rewrite["rejected"], "上海。")

    def test_checkpoint_resumes_and_preserves_previous_output(self):
        config = json.loads((ROOT / "config.mock.json").read_text())
        original_ask = Pipeline.ask

        def fail_second_round(pipeline, role, op, **payload):
            if pipeline.seed == 43 and role == "gpt6" and op == "task":
                raise RuntimeError("simulated interruption")
            return original_ask(pipeline, role, op, **payload)

        with tempfile.TemporaryDirectory() as folder:
            out = Path(folder) / "data.jsonl"
            out.write_text("previous complete output\n")
            with patch.object(Pipeline, "ask", fail_second_round):
                with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                    run(config, [], 2, 42, out)
            self.assertEqual(out.read_text(), "previous complete output\n")
            with sqlite3.connect(str(out) + ".checkpoint.sqlite3") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 1)
            stats = run(config, [], 2, 42, out)
            self.assertGreater(stats["route3"], 0)
            with sqlite3.connect(str(out) + ".checkpoint.sqlite3") as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM rounds").fetchone()[0], 2)
            self.assertTrue(out.read_text().startswith("{"))

    def test_both_questioner_directions_work(self):
        for asker, answerer in (("gpt6", "claude"), ("claude", "gpt6")):
            pair = self.pipeline.route_3(asker, answerer, 0)
            self.assertIsNotNone(pair)
            self.assertNotEqual(pair["chosen"], pair["rejected"])

    def test_review_sample_hides_labels_and_balances_order(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "pairs.jsonl"
            review = Path(folder) / "review.jsonl"
            source.write_text("".join(json.dumps({
                "prompt": f"question {i}", "chosen": f"good {i}",
                "rejected": f"bad {i}", "meta": {"route": "route1", "task_id": str(i)},
            }) + "\n" for i in range(6)))
            with patch.object(sys, "argv", ["audit.py", str(source), "--per-route", "6",
                                            "--review-out", str(review)]):
                with contextlib.redirect_stdout(io.StringIO()):
                    audit.main()
            rows = [json.loads(x) for x in review.read_text().splitlines()]
            key = [json.loads(x) for x in Path(str(review) + ".key.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 6)
            self.assertNotIn("chosen", rows[0])
            self.assertEqual([x["chosen_side"] for x in key].count("a"), 3)


if __name__ == "__main__":
    unittest.main()
