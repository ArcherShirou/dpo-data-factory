#!/usr/bin/env python3
"""Build DPO pairs through user-supplied JSON-over-stdin model commands."""

import argparse
import hashlib
import json
import random
import subprocess
import sys
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def call(command, request, timeout):
    result = subprocess.run(
        command,
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        capture_output=True,
        cwd=ROOT,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {result.stderr[:300]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError(f"command returned invalid JSON: {result.stdout[:300]}") from exc


class Pipeline:
    def __init__(self, config, seed):
        self.config = config
        self.rng = random.Random(seed)
        self.stats = Counter()
        self.seen = set()
        self.timeout = config.get("timeout_seconds", 120)
        for role in ("gpt6", "claude", "target", "verifier"):
            if not config.get("roles", {}).get(role, {}).get("command"):
                raise ValueError(f"missing command for role: {role}")

    def ask(self, role, op, **payload):
        command = self.config["roles"][role]["command"]
        request = {"op": op, "seed": self.rng.randrange(2**32), **payload}
        return call(command, request, self.timeout)

    def task(self, role, index):
        task = self.ask(role, "task", index=index)["task"]
        task.setdefault("id", f"{role}-{index}")
        return task

    def answer(self, role, task, index):
        text = self.ask(role, "answer", task=task, sample_index=index,
                        temperature=self.config.get("sample_temperature", 0.9))["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("empty answer")
        return text.strip()

    def valid_task(self, task):
        if task.get("kind") not in ("open", "objective") or not task.get("prompt"):
            return False
        if task["kind"] == "objective":
            return bool(task.get("reference") and task.get("reference_source")
                        and task["reference_source"] != "model")
        return True

    def verify(self, task, text):
        if task["kind"] == "open":
            return True
        result = self.ask("verifier", "verify", task=task, text=text)
        if type(result.get("correct")) is not bool:
            raise ValueError("verifier must return a boolean correct field")
        return result["correct"]

    def rank(self, judge, task, answers):
        ids = list(range(len(answers)))
        self.rng.shuffle(ids)
        candidates = [{"id": i, "text": answers[i]} for i in ids]
        order = self.ask(judge, "rank", task=task, candidates=candidates)["order"]
        if len(order) != len(ids) or set(order) != set(ids):
            raise ValueError("rank must contain each candidate id exactly once")
        return order

    def compare(self, judge, task, a, b):
        # Reversing presentation catches unstable preferences and position bias.
        winners = []
        for order in ((a, b), (b, a)):
            result = self.ask(judge, "compare", task=task,
                              candidates=[{"id": i, "text": t} for i, t in enumerate(order)])
            winner_id = result.get("winner")
            winners.append(order[winner_id] if type(winner_id) is int and winner_id in (0, 1) else None)
        return winners[0] if winners[0] == winners[1] and winners[0] in (a, b) else None

    def pair(self, task, chosen, rejected, route, judge, corrected=False, trace=None):
        if chosen.strip() == rejected.strip():
            self.stats["skip_identical"] += 1
            return None
        key = hashlib.sha256(json.dumps([task["prompt"], chosen, rejected],
                                        ensure_ascii=False).encode()).hexdigest()
        if key in self.seen:
            self.stats["skip_duplicate"] += 1
            return None
        self.seen.add(key)
        self.stats[route] += 1
        return {"prompt": task["prompt"], "chosen": chosen, "rejected": rejected,
                "meta": {"route": route, "task_id": task["id"], "judge": judge,
                         "kind": task["kind"], "corrected": corrected,
                         "synthetic_demo": self.config.get("synthetic_demo", False),
                         "reference_source": task.get("reference_source"),
                         "trace": trace or {}}}

    def decide(self, task, preferred, other, route, judge, corrector,
               confirmed=False, trace=None):
        if task["kind"] == "objective":
            if not self.verify(task, preferred):
                fix = self.ask(corrector, "correct", task=task, rejected=preferred)["text"]
                if not self.verify(task, fix):
                    self.stats["skip_failed_correction"] += 1
                    return None
                return self.pair(task, fix, preferred, route, judge,
                                 corrected=True, trace=trace)
            if self.verify(task, other) and not confirmed:
                winner = self.compare(judge, task, preferred, other)
                if winner is None:
                    self.stats["skip_unstable_judge"] += 1
                    return None
                preferred, other = (preferred, other) if winner == preferred else (other, preferred)
        elif not confirmed:
            winner = self.compare(judge, task, preferred, other)
            if winner is None:
                self.stats["skip_unstable_judge"] += 1
                return None
            preferred, other = (preferred, other) if winner == preferred else (other, preferred)
        if task["kind"] == "objective" and not self.verify(task, preferred):
            self.stats["skip_invalid_chosen"] += 1
            return None
        return self.pair(task, preferred, other, route, judge, trace=trace)

    def routes_1_2(self, task):
        if not self.valid_task(task):
            self.stats["skip_untrusted_task"] += 1
            return []
        answers = list(dict.fromkeys(self.answer("target", task, i) for i in range(5)))
        if len(answers) < 2:
            self.stats["skip_no_contrast"] += 1
            return []
        order = self.rank("gpt6", task, answers)
        top, bottom = answers[order[0]], answers[order[-1]]
        out = []
        first = self.decide(task, top, bottom, "route1", "gpt6", "gpt6",
                            trace={"candidates": answers, "rank_order": order})
        if first:
            out.append(first)
        rewrite = self.ask("gpt6", "rewrite", task=task, original=top)["text"]
        winner = self.compare("claude", task, top, rewrite)
        if winner is None:
            self.stats["skip_unstable_judge"] += 1
        else:
            second = self.decide(task, winner,
                                 rewrite if winner == top else top,
                                 "route2", "claude", "gpt6", confirmed=True,
                                 trace={"original": top, "rewrite": rewrite})
            if second:
                out.append(second)
        return out

    def route_3(self, asker, answerer, index):
        task = self.task(asker, index)
        if not self.valid_task(task):
            self.stats["skip_untrusted_task"] += 1
            return None
        answers = list(dict.fromkeys(self.answer(answerer, task, i) for i in range(3)))
        if len(answers) < 2:
            self.stats["skip_no_contrast"] += 1
            return None
        order = self.rank(asker, task, answers)
        return self.decide(task, answers[order[0]], answers[order[-1]],
                           "route3", asker, asker,
                           trace={"answerer": answerer, "candidates": answers,
                                  "rank_order": order})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.mock.json")
    parser.add_argument("--tasks", type=Path, default=ROOT / "tasks.example.jsonl")
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=ROOT / "data.jsonl")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")
    config = json.loads(args.config.read_text())
    pipeline = Pipeline(config, args.seed)
    tasks = [json.loads(line) for line in args.tasks.read_text().splitlines() if line.strip()]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as output:
        for i in range(args.count):
            task = tasks[i] if i < len(tasks) else pipeline.task("gpt6", i)
            task.setdefault("id", f"seed-{i}")
            for pair in pipeline.routes_1_2(task):
                output.write(json.dumps(pair, ensure_ascii=False) + "\n")
            for asker, answerer in (("gpt6", "claude"), ("claude", "gpt6")):
                pair = pipeline.route_3(asker, answerer, i)
                if pair:
                    output.write(json.dumps(pair, ensure_ascii=False) + "\n")
    print(json.dumps({"out": str(args.out), "stats": pipeline.stats}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
