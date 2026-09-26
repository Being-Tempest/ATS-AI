"""
点名保真率（红审4.5 P0，预注册 §8.2.4）

λ=24_drift 种子 42 的 full_merge / full_merge_q3 两臂，批量 prompt 走真实 DeepSeek
生成（ApiCache merge_gen），单条回复仍占位。自动计算：
  - ID 保真率：covered 用户名在批量回应文本中出现（精确子串 / 观众NN→数字 / ≥2字后缀）
  - 话题保真率：covered 弹幕的内容 bigram（CJK+数字，去停用）在回应中出现的条目比例
人工锚点：前 5 个批量事件逐条人工判读（实验者），核对自动指标方向。

用法：python -X utf8 -m benchmark.merge_fidelity
输出：benchmark/results/decisions_fidelity_ovl_l24_drift_<arm>.jsonl
      benchmark/results/merge_fidelity_check.md
"""

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark.replay import (load_events_file, prescore_events, replay_scheduler,
                                write_jsonl, RESULTS_DIR, STUB_REPLY)
from benchmark.api_cache import ApiCache

SRC = "ovl_l24_drift"
STOP_CHARS = set("的了吗呢啊我你他这那就也都在是有不没好么什怎为谁哪哈啦呀吧哦嗯嘛啦咯哩观众主播")
CJK = re.compile(r"[一-鿿0-9A-Za-z]")


def name_hit(username: str, reply: str) -> bool:
    if username and username in reply:
        return True
    m = re.fullmatch(r"观众(\d+)", username)
    if m:
        return m.group(1).lstrip("0") in reply or m.group(1) in reply
    if len(username) >= 3 and username[-2:] in reply:
        return True
    return False


def content_bigrams(text: str) -> set[str]:
    chars = [c for c in text if CJK.match(c)]
    grams = set()
    for i in range(len(chars) - 1):
        g = "".join(chars[i:i + 2])
        if g[0] not in STOP_CHARS and g[1] not in STOP_CHARS:
            grams.add(g)
    return grams


def batch_fidelity(decision) -> dict:
    covered = decision.get("covered", [])
    reply = decision.get("reply", "")
    dms = [c for c in covered if c["type"] == "danmaku"]
    id_hits = sum(1 for c in covered if name_hit(c["username"], reply))
    topic_items = [c for c in dms if content_bigrams(c["text"])]
    topic_hits = sum(1 for c in topic_items
                     if any(g in reply for g in content_bigrams(c["text"])))
    return {
        "n": len(covered),
        "id_rate": id_hits / len(covered) if covered else None,
        "topic_rate": topic_hits / len(topic_items) if topic_items else None,
        "id_hits": id_hits, "topic_hits": topic_hits, "topic_items": len(topic_items),
    }


MANUAL = []  # (样本序号, 人工判读ID, 人工判读话题, 备注)


async def run_arm(arm, cache):
    events = load_events_file(os.path.join("benchmark", "data", f"{SRC}.jsonl"))
    scores = await prescore_events(events, ApiCache("score_danmaku", None, enabled=False))

    async def fidelity_reply(username, text):
        if username.startswith("（批量/"):
            return (await cache(text), "default_smile")
        return (STUB_REPLY, "default_smile")

    q3 = 8 if arm == "full_merge_q3" else None
    decisions, trace, sched = await replay_scheduler(
        events, scores, "fidelity", "full", variant=arm,
        batch_merge=True, batch_q3_trigger=q3, reply_fn=fidelity_reply)
    out = os.path.join(RESULTS_DIR, f"decisions_fidelity_{SRC}_{arm}.jsonl")
    write_jsonl(out, decisions)
    n_batch = sum(1 for d in decisions if d["type"] == "batch")
    print(f"[{arm}] 回复 {len(decisions)} 条，批量 {n_batch} 次", flush=True)
    return decisions


async def main():
    async def _gen(prompt: str) -> str:
        from openai import AsyncOpenAI
        from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
        from persona import SYSTEM_PROMPT
        client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY, base_url=DEEPSEEK_BASE_URL)
        resp = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": prompt}],
            max_tokens=200, temperature=0.9,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return (resp.choices[0].message.content or "").strip()

    cache = ApiCache("merge_gen", _gen, enabled=True)
    arms = {}
    for arm in ("full_merge", "full_merge_q3"):
        arms[arm] = await run_arm(arm, cache)
    print(cache.stats(), flush=True)

    lines = [
        "# 点名保真率检验（预注册 §8.2.4，λ=24_drift 种子 42，真实生成）",
        "",
        "- ID 保真规则：用户名精确子串命中；`观众NN`→数字命中；≥3 字用户名→末 2 字后缀命中",
        "- 话题保真规则：弹幕内容 bigram（CJK+数字，去停用字）任一在回应中出现即算被提及；"
        "无内容 bigram 的弹幕（如「666」）不进分母",
        "",
    ]
    for arm, decisions in arms.items():
        batches = [d for d in decisions if d["type"] == "batch"]
        fids = [(d, batch_fidelity(d)) for d in batches]
        id_rates = [f["id_rate"] for _, f in fids if f["id_rate"] is not None]
        topic_rates = [f["topic_rate"] for _, f in fids if f["topic_rate"] is not None]
        tot_id_h = sum(f["id_hits"] for _, f in fids)
        tot_id_n = sum(f["n"] for _, f in fids)
        tot_tp_h = sum(f["topic_hits"] for _, f in fids)
        tot_tp_n = sum(f["topic_items"] for _, f in fids)
        lines += [
            f"## `{arm}`（批量 {len(batches)} 次）",
            "",
            f"- **ID 保真率**：批均 {sum(id_rates)/len(id_rates)*100:.1f}%"
            f"（条目级 {tot_id_h}/{tot_id_n} = {tot_id_h/tot_id_n*100:.1f}%）" if id_rates else "- 无批量",
            f"- **话题保真率**：批均 {sum(topic_rates)/len(topic_rates)*100:.1f}%"
            f"（条目级 {tot_tp_h}/{tot_tp_n} = {tot_tp_h/tot_tp_n*100:.1f}%）" if topic_rates else "",
            "",
            "| # | 类型 | 覆盖 | ID保真 | 话题保真 | 回应（截 50 字） |",
            "|---|---|---|---|---|---|",
        ]
        for i, (d, f) in enumerate(fids, 1):
            lines.append(
                f"| {i} | {d['batch_kind']} | {f['n']} | "
                f"{'—' if f['id_rate'] is None else f'{f['id_rate']*100:.0f}%'} | "
                f"{'—' if f['topic_rate'] is None else f'{f['topic_rate']*100:.0f}%'} | "
                f"{d['reply'][:50]} |")
        lines.append("")

    # 人工锚点：full_merge 前 5 个批量，逐条人工判读
    lines += [
        "## 人工校准锚点（实验者判读，full_merge 前 5 批）",
        "",
        "判读口径：ID 保真=回应中可辨认地点名了几个 covered 观众；话题保真=covered 弹幕的话题"
        "是否被概括到（允许同义改写，不要求原词）。",
        "",
    ]
    batches = [d for d in arms["full_merge"] if d["type"] == "batch"][:5]
    for i, d in enumerate(batches, 1):
        f = batch_fidelity(d)
        cov_desc = "；".join(f"{c['username']}:{c['text'][:15]}" for c in d["covered"][:6])
        lines += [
            f"### 锚点 {i}（{d['batch_kind']}，覆盖 {len(d['covered'])} 条）",
            "",
            f"- covered：{cov_desc}",
            f"- 生成：{d['reply']}",
            f"- 自动指标：ID {f['id_hits']}/{f['n']}，话题 {f['topic_hits']}/{f['topic_items']}",
            f"- 人工判读：__待填__",
            "",
        ]
    out = os.path.join(RESULTS_DIR, "merge_fidelity_check.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
