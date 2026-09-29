"""包1：回复质量人类效度标注材料包（老师意见五-3）

现有人评（blind_200）验的是"消息价值"，不是"回复质量"。本包抽取 45 条已生成回复
（15 greedy 精确 + 15 主臂精确 + 15 批量回复），打乱去标识供同学盲标 1-5 质量分；
同时用 LLM judge 对同样 45 条回复打质量分（缓存 reply_judge），回收后人评均值
与 judge 分算 Spearman 相关，补上回复质量维度的人类效度。

来源：
- 精确回复：benchmark/results/precise_fidelity_samples.jsonl（v3 保真轮真实生成，
  两臂各 60 条，arm 字段标识，标注表不展示）
- 批量回复：benchmark/annotation/batch_blind.csv（25 条真实生成批量回应，含上下文弹幕）

用法：python -X utf8 -m benchmark.annotation.make_reply_quality_pack
输出：benchmark/annotation/reply_quality/
      reply_quality_blind45.csv   标注表（发给同学）
      README.md                   标注者说明
      .reply_quality_key.json     隐藏 key（编号→臂/来源/judge 分，勿发给同学）
缓存：benchmark/cache/reply_judge.jsonl（45 次真实调用，重跑零额外调用）
"""
import asyncio
import csv
import json
import os
import random
import re

sys_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sys
sys.path.insert(0, sys_path)

from benchmark.api_cache import ApiCache

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(sys_path, "benchmark", "results")
OUT = os.path.join(HERE, "reply_quality")
N_PER_CLASS = 15
SEED = 42

TAG_RE = re.compile(r"^\[[^\[\]]{1,8}\]\s*")

JUDGE_PROMPT = """你是直播回复质量评审。给定弹幕原文和 AI 主播的回复，给回复质量打一个 0-80 的整数。

评分标准：

60-80: 自然贴切，像真人主播（准确接住弹幕内容、有温度、口语自然、分寸恰当）
40-59: 相关但平淡（答到了内容，但套话感重、缺乏个性或情感连接）
20-39: 勉强相关或机械（只蹭到边、模板化明显、上下文接得生硬）
0-19: 离题或错乱（答非所问、读不懂上下文、事实张冠李戴）

你只回复一个 0-80 的整数，不要任何解释。"""


def strip_tag(reply: str) -> str:
    return TAG_RE.sub("", reply).strip()


def load_samples():
    samples = []
    rows = [json.loads(l) for l in open(
        os.path.join(RES, "precise_fidelity_samples.jsonl"), encoding="utf-8") if l.strip()]
    rng = random.Random(SEED)
    for arm, label in (("greedy", "greedy_precise"), ("full_merge_commit", "main_precise")):
        pool = [r for r in rows if r["arm"] == arm]
        rng.shuffle(pool)
        for r in pool[:N_PER_CLASS]:
            samples.append({"cls": label, "source": "precise_fidelity_samples",
                            "context": f"{r['username']}：{r['text']}",
                            "reply": strip_tag(r["reply"])})
    with open(os.path.join(HERE, "batch_blind.csv"), encoding="utf-8-sig") as f:
        batch_rows = [r for r in csv.DictReader(f) if r.get("batch_reply")]
    rng.shuffle(batch_rows)
    for r in batch_rows[:N_PER_CLASS]:
        samples.append({"cls": "batch", "source": "batch_blind",
                        "context": r["context_danmaku"].replace("；", "\n"),
                        "reply": strip_tag(r["batch_reply"])})
    rng.shuffle(samples)
    for i, s in enumerate(samples, 1):
        s["rid"] = f"R{i:02d}"
    return samples


async def judge_all(samples):
    from openai import AsyncOpenAI
    from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
    client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY or "offline-cache-only", base_url=DEEPSEEK_BASE_URL)

    async def _judge(prompt: str) -> int:
        resp = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "system", "content": JUDGE_PROMPT},
                      {"role": "user", "content": prompt}],
            max_tokens=200, temperature=0.0,
            extra_body={"thinking": {"type": "disabled"}},
        )
        text = (resp.choices[0].message.content or "").strip()
        m = re.search(r"\d+", text)
        return int(m.group(0)) if m else -1

    cache = ApiCache("reply_judge", _judge, enabled=True)
    for s in samples:
        prompt = f"弹幕原文：\n{s['context']}\n\nAI 主播的回复：\n{s['reply']}"
        s["judge_score"] = await cache(prompt)
    print(cache.stats(), flush=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    samples = load_samples()
    asyncio.run(judge_all(samples))

    csv_path = os.path.join(OUT, "reply_quality_blind45.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["编号", "弹幕原文", "回复内容", "质量评分1-5", "备注"])
        for s in samples:
            w.writerow([s["rid"], s["context"], s["reply"], "", ""])

    key = {s["rid"]: {"cls": s["cls"], "source": s["source"],
                      "judge_score": s["judge_score"]} for s in samples}
    with open(os.path.join(OUT, ".reply_quality_key.json"), "w", encoding="utf-8") as f:
        json.dump(key, f, ensure_ascii=False, indent=1)

    print(f"→ {csv_path}（{len(samples)} 条）")
    from collections import Counter
    print(Counter(s["cls"] for s in samples))
    print("judge 分布:", sorted(s["judge_score"] for s in samples))


if __name__ == "__main__":
    main()
