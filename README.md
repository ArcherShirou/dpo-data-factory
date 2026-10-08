# dpo-data-factory

用几条不同的路线生成 DPO 偏好对。仓库自带三个假模型，下载后可以直接跑；真正的模型由你接入。

```bash
python3 dpo_factory.py --count 2 --out data.jsonl
python3 -m unittest -q
```

运行结果是 JSONL，每行都有 `prompt`、`chosen`、`rejected` 和 `meta`。假模型的记录带有 `meta.synthetic_demo=true`，只用来检查流程，别拿去训练。

## 三条路线

1. GPT 角色出题；目标模型独立回答 5 次；GPT 角色排序，比较第一名和最后一名。
2. GPT 角色改写第一条路线中的最高分回答；Claude 角色盲评原文与改写，交换展示顺序再评一次。两次不一致就丢弃。
3. GPT 和 Claude 轮流出题。回答方各给出至少 3 个答案，出题方排序，比较第一名和最后一名。两个方向都会运行。

开放题的首尾答案还会交换顺序复评。排序不稳定、答案相同或成对数据重复时，直接跳过，不为了凑数硬造偏好。

**有标准答案的题目先验对错。** 题目必须带 `reference` 和非模型自述的 `reference_source`，并配置独立的 `verifier` 命令。如果排名第一的回答错了，它会成为 `rejected`；出题模型依据参考答案写出修正版，只有修正版通过同一个验答器，才会成为 `chosen`。如果排名第一和最后一名都正确，再看交换顺序后的偏好结果。验答器的可靠性决定这类数据的上限。

## 换成自己的模型

复制 `config.mock.json`，把四个 `command` 换成自己的可执行命令。`gpt6`、`claude`、`target` 是流程中的角色名，不限制你使用哪家模型或哪个版本。`verifier` 应尽量用答案键、测试程序或可核实的证据实现，不建议再用同一个裁判模型充当事实真值。

每个命令从标准输入读一条 JSON 请求，向标准输出写一条 JSON 响应。错误写到标准错误并返回非零退出码。进程每次请求单独启动，工作目录是仓库根目录；`seed` 会随请求传入，回答请求还会带 `temperature`。

| `op` | 请求中的主要字段 | 响应 |
| --- | --- | --- |
| `task` | `index` | `{"task":{"id":"...","kind":"open","prompt":"..."}}` |
| `answer` | `task`, `sample_index`, `temperature` | `{"text":"..."}` |
| `rank` | `task`, `candidates:[{"id":0,"text":"..."}]` | `{"order":[2,0,1]}`，从好到差 |
| `compare` | `task`, 两个 `candidates` | `{"winner":0}`，返回候选 id |
| `rewrite` | `task`, `original` | `{"text":"..."}` |
| `correct` | `task`, `rejected` | `{"text":"..."}` |
| `verify` | `task`, `text` | `{"correct":true}` 或 `false` |

一个角色命令可以只实现它会收到的操作。`gpt6` 需要 `task/answer/rank/compare/rewrite/correct`；`claude` 需要 `task/answer/rank/compare`；`target` 只需 `answer`；`verifier` 只需 `verify`。`mock_model.py` 是最小可运行例子。密钥由你自己的命令管理，不写进本仓库的配置文件。

题目文件是 JSONL，默认读取 `tasks.example.jsonl`；文件中的题先用完，再由 GPT 角色生成题。开放题示例：

```json
{"id":"mail-001","kind":"open","prompt":"写一封简短的请假邮件。"}
```

有标准答案的题需要可信参考答案来源。仓库里的“中国首都”只是演示：假裁判会故意把“上海”排第一，验证修正分支是否生效。生产数据请用自己的题库和验答器。

```bash
python3 dpo_factory.py --config config.my-models.json --tasks my-tasks.jsonl --count 100 --out my-pairs.jsonl
```

`--count` 是每条路线处理的题目轮数，不是最终输出行数。`meta.trace` 留下候选、排序和改写前后的文本，方便抽查；输出里不会保存模型密钥。正式训练前，按题目来源去重并按题目分组切分训练集和验证集，人工抽查一部分样本，保留独立的评测集。有关设计依据与取舍见 [DESIGN.md](DESIGN.md)。
