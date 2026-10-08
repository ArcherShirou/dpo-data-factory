import json
import unittest
from pathlib import Path

from dpo_factory import Pipeline


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

    def test_both_questioner_directions_work(self):
        for asker, answerer in (("gpt6", "claude"), ("claude", "gpt6")):
            pair = self.pipeline.route_3(asker, answerer, 0)
            self.assertIsNotNone(pair)
            self.assertNotEqual(pair["chosen"], pair["rejected"])


if __name__ == "__main__":
    unittest.main()
