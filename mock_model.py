#!/usr/bin/env python3
"""Deterministic stand-ins for three model roles and an external verifier."""

import json
import sys


def task(role, index):
    prompts = {
        "gpt6": ["写一封简短邮件，婉拒本周的会议邀请。", "写一份清晰的 bug 报告，描述设置页保存昵称失败。"],
        "claude": ["给同事写一条消息，说明你无法参加明天的讨论。", "向实习生解释如何整理会议纪要。"],
    }
    return {"id": f"{role}-{index}", "kind": "open",
            "prompt": prompts[role][index % len(prompts[role])]}


def answer(role, item, index):
    if item["kind"] == "objective":
        answers = ["上海。", "北京。", "中国的首都是北京。", "不太确定。", "广州。"]
        return answers[index % len(answers)]
    prompt = item["prompt"]
    if "bug 报告" in prompt:
        answers = [
            "软件坏了。", "保存时会报错。",
            "标题：设置页保存失败。环境：版本 1.2、macOS。复现步骤：修改昵称后点保存。预期结果：保存成功。实际结果：提示 500 错误。",
            "我点保存没反应。", "版本 1.2 的设置页保存昵称时报 500，附上截图和发生时间。",
        ]
    elif "会议纪要" in prompt:
        answers = [
            "会开完了。", "记录讨论主题和结论。",
            "会议纪要：写明议题、结论、待办事项、负责人和截止时间；未决定的问题单独列出。",
            "会上说了很多。", "纪要应记下结论、待办事项及负责人，发给参会者核对。",
        ]
    elif "明天的讨论" in prompt:
        answers = [
            "不去。", "谢谢邀请，我明天无法参加讨论。",
            "谢谢邀请。我明天无法参加讨论；请会后把结论发给我，我会及时跟进。",
            "我有事。", "明天无法到场。如果需要，我可以先发书面意见供大家讨论。",
        ]
    else:
        answers = [
            "好的。", "谢谢你的邀请，我这周无法参加会议。",
            "谢谢邀请。很抱歉我本周无法参加会议；请会后发我纪要，我会及时跟进。",
            "我有事。", "感谢通知，本周我不能参加。若有需要我提前处理的事项，请告诉我。",
        ]
    return answers[index % len(answers)]


def score(text, item):
    if item["kind"] == "objective":
        # Deliberately rank a wrong answer first to exercise the correction gate.
        return {"上海。": 10, "中国的首都是北京。": 8, "北京。": 7,
                "广州。": 1, "不太确定。": 0}.get(text, 5)
    if "复现步骤" in text:
        return 10 if "临时处理" in text else 9
    if "负责人和截止时间" in text:
        return 10 if "发送时间" in text else 9
    if "会后把结论" in text:
        return 10 if "书面意见" in text else 9
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
        if item["kind"] == "objective":
            rewritten = "北京是中国的首都。"
        elif "bug 报告" in item["prompt"]:
            rewritten = "标题：设置页保存失败。环境：版本 1.2、macOS。复现步骤：修改昵称后点保存。预期结果：保存成功。实际结果：提示 500 错误。临时处理：重试无效。"
        elif "会议纪要" in item["prompt"]:
            rewritten = "会议纪要：写明议题、结论、待办事项、负责人和截止时间；未决定的问题单独列出。发送时间：会后当天。"
        elif "明天的讨论" in item["prompt"]:
            rewritten = "谢谢邀请。我明天无法参加讨论；请会后把结论发给我，我会及时跟进。如需书面意见，我可以提前发给大家。"
        else:
            rewritten = "谢谢邀请。很抱歉我本周无法参加会议；请会后发我纪要，我会及时跟进。如需我提前处理事项，也请告诉我。"
        result = {"text": rewritten}
    elif op == "correct":
        result = {"text": "中国的首都是北京。" if item["kind"] == "objective"
                  else "请根据任务要求提供准确、完整的回答。"}
    elif op == "verify":
        # Only the bundled fixture is covered; replace this command for real tasks.
        result = {"correct": item.get("id") == "china-capital"
                  and "北京" in request["text"] and "不是北京" not in request["text"],
                  "evidence": "fixture answer key: china-capital=北京"}
    elif op == "validate_reference":
        result = {"trusted": item.get("id") == "china-capital"
                  and item.get("reference") == "北京"
                  and item.get("reference_source") == "demo-fixture",
                  "evidence": "fixture answer key: china-capital=北京"}
    elif op == "quality":
        acceptable = score(request["text"], item) >= 5
        result = {"acceptable": acceptable,
                  "reason": "mock rubric: sufficiently answers prompt" if acceptable
                  else "mock rubric: incomplete answer"}
    else:
        raise ValueError(f"unknown operation: {op}")
    json.dump(result, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
