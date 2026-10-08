#!/usr/bin/env python3
"""Build DPO pairs through user-supplied JSON-over-stdin model commands."""

import argparse
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def pair_key(prompt, chosen, rejected):
    return hashlib.sha256(json.dumps([prompt, chosen, rejected],
                                    ensure_ascii=False).encode()).hexdigest()


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
        self.seed = seed
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
        if task.get("kind") not in ("open", "objective") or not isinstance(task.get("prompt"), str) or not task["prompt"].strip():
            return False
        if task["kind"] == "objective":
            if not all(isinstance(task.get(field), str) and task[field].strip()
                       for field in ("reference", "reference_source")):
                return False
            result = self.ask("verifier", "validate_reference", task=task)
            if (type(result.get("trusted")) is not bool or
                    not isinstance(result.get("evidence"), str) or not result["evidence"].strip()):
                raise ValueError("validate_reference must return trusted and evidence")
            task["reference_evidence"] = result["evidence"]
            return result["trusted"]
        return True

    def verdict(self, task, text):
        op, field, reason = (("verify", "correct", "evidence") if task["kind"] == "objective"
                             else ("quality", "acceptable", "reason"))
        result = self.ask("verifier", op, task=task, text=text)
        if (type(result.get(field)) is not bool or
                not isinstance(result.get(reason), str) or not result[reason].strip()):
            raise ValueError(f"{op} must return boolean {field} and nonempty {reason}")
        return {"passed": result[field], "reason": result[reason]}

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
        winner = winners[0] if winners[0] == winners[1] and winners[0] in (a, b) else None
        return winner, winners

    def pair(self, task, chosen, rejected, route, judge, corrected=False, trace=None):
        if chosen.strip() == rejected.strip():
            self.stats["skip_identical"] += 1
            return None
        key = pair_key(task["prompt"], chosen, rejected)
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
                         "reference_evidence": task.get("reference_evidence"),
                         "run_seed": self.seed,
                         "models": {role: data.get("model_id", role)
                                    for role, data in self.config["roles"].items()},
                         "trace": trace or {}}}

    def decide(self, task, preferred, other, route, judge, corrector, trace=None):
        trace = dict(trace or {})
        if task["kind"] == "objective":
            preferred_verdict = self.verdict(task, preferred)
            if not preferred_verdict["passed"]:
                fix = self.ask(corrector, "correct", task=task, rejected=preferred)["text"]
                if not isinstance(fix, str) or not fix.strip():
                    self.stats["skip_failed_correction"] += 1
                    return None
                fix_verdict = self.verdict(task, fix)
                trace["verdicts"] = {"chosen": fix_verdict, "rejected": preferred_verdict}
                if not fix_verdict["passed"]:
                    self.stats["skip_failed_correction"] += 1
                    return None
                return self.pair(task, fix, preferred, route, judge,
                                 corrected=True, trace=trace)
            other_verdict = self.verdict(task, other)
            if other_verdict["passed"]:
                winner, votes = self.compare(judge, task, preferred, other)
                trace["compare_votes"] = votes
                if winner is None:
                    self.stats["skip_unstable_judge"] += 1
                    return None
                if winner == other:
                    preferred, other = other, preferred
                    preferred_verdict, other_verdict = other_verdict, preferred_verdict
            trace["verdicts"] = {"chosen": preferred_verdict, "rejected": other_verdict}
        else:
            winner, votes = self.compare(judge, task, preferred, other)
            trace["compare_votes"] = votes
            if winner is None:
                self.stats["skip_unstable_judge"] += 1
                return None
            preferred, other = (preferred, other) if winner == preferred else (other, preferred)
            chosen_verdict = self.verdict(task, preferred)
            trace["verdicts"] = {"chosen": chosen_verdict}
            if not chosen_verdict["passed"]:
                self.stats["skip_low_quality_chosen"] += 1
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
        if not isinstance(rewrite, str) or not rewrite.strip() or rewrite.strip() == top:
            self.stats["skip_no_improvement"] += 1
            return out
        rewrite_verdict = self.verdict(task, rewrite)
        if not rewrite_verdict["passed"]:
            self.stats["skip_bad_rewrite"] += 1
            return out
        original_verdict = self.verdict(task, top)
        winner, votes = self.compare("claude", task, top, rewrite)
        factual_override = task["kind"] == "objective" and not original_verdict["passed"]
        if not factual_override and winner != rewrite:
            self.stats["skip_no_improvement"] += 1
            return out
        trace = {"original": top, "rewrite": rewrite, "compare_votes": votes,
                 "verdicts": {"chosen": rewrite_verdict, "rejected": original_verdict},
                 "factual_override": factual_override}
        second = self.pair(task, rewrite, top, "route2",
                           "verifier" if factual_override else "claude", trace=trace)
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
    tasks = [json.loads(line) for line in args.tasks.read_text().splitlines() if line.strip()]
    stats = run(config, tasks, args.count, args.seed, args.out)
    print(json.dumps({"out": str(args.out), "stats": stats}, ensure_ascii=False))


def run(config, tasks, count, seed, out):
    """Commit each round to SQLite, then atomically publish the JSONL file."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    signature = hashlib.sha256(json.dumps(
        {"config": config, "tasks": tasks, "count": count, "seed": seed,
         "implementation": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    checkpoint = Path(str(out) + ".checkpoint.sqlite3")
    with sqlite3.connect(checkpoint) as db:
        db.execute("CREATE TABLE IF NOT EXISTS run (signature TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS rounds (idx INTEGER PRIMARY KEY, pairs TEXT NOT NULL, stats TEXT NOT NULL)")
        saved = db.execute("SELECT signature FROM run").fetchone()
        if saved and saved[0] != signature:
            raise ValueError(f"checkpoint belongs to another run: {checkpoint}")
        if not saved:
            db.execute("INSERT INTO run VALUES (?)", (signature,))
            db.commit()
        rounds = {i: (json.loads(pairs), json.loads(round_stats)) for i, pairs, round_stats
                  in db.execute("SELECT idx, pairs, stats FROM rounds")}
        seen = {pair_key(p["prompt"], p["chosen"], p["rejected"])
                for pairs, _ in rounds.values() for p in pairs}
        totals = Counter()
        for i in range(count):
            if i in rounds:
                totals.update(rounds[i][1])
                continue
            pipeline = Pipeline(config, seed + i)
            pipeline.seen = seen
            task = dict(tasks[i]) if i < len(tasks) else pipeline.task("gpt6", i)
            task.setdefault("id", f"seed-{i}")
            pairs = pipeline.routes_1_2(task)
            for asker, answerer in (("gpt6", "claude"), ("claude", "gpt6")):
                pair = pipeline.route_3(asker, answerer, i)
                if pair:
                    pairs.append(pair)
            db.execute("INSERT INTO rounds VALUES (?, ?, ?)",
                       (i, json.dumps(pairs, ensure_ascii=False),
                        json.dumps(pipeline.stats, ensure_ascii=False)))
            db.commit()
            totals.update(pipeline.stats)
            rounds[i] = (pairs, dict(pipeline.stats))
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile("w", dir=out.parent, prefix=out.name + ".",
                                         delete=False) as output:
            temp_path = output.name
            for i in range(count):
                for pair in rounds[i][0]:
                    output.write(json.dumps(pair, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, out)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
    return totals


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
