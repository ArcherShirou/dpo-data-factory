#!/usr/bin/env python3
"""Deterministic stand-ins for three model roles and an external verifier."""

import json
import sys


def task(role, index):
    prompts = {
        "gpt6": ["写一封简短邮件，婉拒本周的会议邀请。", "给新同事解释如何写清晰的 bug 报告。"],
        "claude": ["给同事写一条消息，说明你无法参加明天的讨论。", "向实习生解释如何整理会议纪要。"],
    }
    return {"id": f"{role}-{index}", "kind": "open",
            "prompt": prompts[role][index % len(prompts[role])]}


def answer(role, item, index):
    if item["kind"] == "objective":
        answers = ["上海。", "北京。", "中国的首都是北京。", "不太确定。", "广州。"]
        return answers[index % len(answers)]
    answers = [
        "好的。",
        "谢谢你的邀请，我这周无法参加会议。",
        "谢谢邀请。很抱歉我本周无法参加会议；请会后发我纪要，我会及时跟进。",
        "我有事。",
        "感谢通知，本周我不能参加。若有需要我提前处理的事项，请告诉我。",
    ]
    return answers[index % len(answers)]


def score(text, item):
    if item["kind"] == "objective":
        # Deliberately rank a wrong answer first to exercise the correction gate.
        return {"上海。": 10, "中国的首都是北京。": 8, "北京。": 7,
                "广州。": 1, "不太确定。": 0}.get(text, 5)
    if "请会后发我纪要" in text:
        return 10 if "提前处理" in text else 9
    if "提前处理" in text:
        return 8
    if "很抱歉" in text:
        return 7
    if "谢谢" in text or "感谢" in text:
        return 5
    return 1


def main():
    role = sys.argv[1]
    request = json.load(sys.stdin)
    op = request["op"]
    item = request.get("task", {})
    if op == "task":
        result = {"task": task(role, request["index"])}
    elif op == "answer":
        result = {"text": answer(role, item, request["sample_index"])}
    elif op == "rank":
        candidates = request["candidates"]
        result = {"order": [c["id"] for c in sorted(
            candidates, key=lambda c: score(c["text"], item), reverse=True)]}
    elif op == "compare":
        candidates = request["candidates"]
        winner = max(candidates, key=lambda c: (
            "北京" in c["text"] if item["kind"] == "objective" else False,
            score(c["text"], item)))
        result = {"winner": winner["id"]}
    elif op == "rewrite":
        result = {"text": "北京是中国的首都。" if item["kind"] == "objective"
                  else "谢谢邀请。很抱歉我本周无法参加会议；请会后发我纪要，我会及时跟进。如需我提前处理事项，也请告诉我。"}
    elif op == "correct":
        result = {"text": "中国的首都是北京。" if item["kind"] == "objective"
                  else "请根据任务要求提供准确、完整的回答。"}
    elif op == "verify":
        # Only the bundled fixture is covered; replace this command for real tasks.
        result = {"correct": item.get("id") == "china-capital"
                  and "北京" in request["text"] and "不是北京" not in request["text"]}
    else:
        raise ValueError(f"unknown operation: {op}")
    json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
