#!/usr/bin/env python3
"""Summarize a DPO JSONL file and sample pairs for human review."""

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--per-route", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--review-out", type=Path, default=Path("review.jsonl"))
    args = parser.parse_args()
    if args.per_route < 1:
        parser.error("--per-route must be at least 1")
    groups = defaultdict(list)
    for line in args.input.read_text().splitlines():
        if line.strip():
            pair = json.loads(line)
            groups[pair["meta"]["route"]].append(pair)
    rng = random.Random(args.seed)
    args.review_out.parent.mkdir(parents=True, exist_ok=True)
    key_path = Path(str(args.review_out) + ".key.jsonl")
    samples = [(route, pair) for route in sorted(groups)
               for pair in rng.sample(groups[route], min(args.per_route, len(groups[route])))]
    rng.shuffle(samples)
    sides = ["a"] * ((len(samples) + 1) // 2) + ["b"] * (len(samples) // 2)
    rng.shuffle(sides)
    with args.review_out.open("w") as output, key_path.open("w") as key_file:
        for index, ((route, pair), chosen_side) in enumerate(zip(samples, sides)):
            answers = ((pair["chosen"], pair["rejected"]) if chosen_side == "a"
                       else (pair["rejected"], pair["chosen"]))
            pair_id = f"review-{index:06d}"
            output.write(json.dumps({
                "pair_id": pair_id, "prompt": pair["prompt"],
                "answer_a": answers[0], "answer_b": answers[1],
                "human_preferred": "", "answer_a_acceptable": "",
                "answer_b_acceptable": "", "notes": "",
            }, ensure_ascii=False) + "\n")
            key_file.write(json.dumps({"pair_id": pair_id, "route": route,
                                       "task_id": pair["meta"]["task_id"],
                                       "chosen_side": chosen_side}, ensure_ascii=False) + "\n")
    print(json.dumps({"pairs_by_route": {route: len(rows) for route, rows in groups.items()},
                      "corrected": sum(p["meta"].get("corrected", False)
                                       for rows in groups.values() for p in rows),
                      "synthetic_demo": sum(p["meta"].get("synthetic_demo", False)
                                            for rows in groups.values() for p in rows),
                      "review_out": str(args.review_out),
                      "review_key": str(key_path)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
