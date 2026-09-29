"""红审 v3 洞 2：精确回复保真度 f 实测（greedy / 主臂对称口径）。

λ=12_drift 种子 42 的两臂精回消息各等距抽 60 条，用 DeepSeek 真实生成逐条回复
（prompt 与生产管线同格式 "username：text"，persona 系统提示，温度 0.9），
按 merge_fidelity 同口径算 ID 保真（name_hit）与话题保真（content_bigrams）。
缓存命名空间 precise_gen，重复运行零额外调用。

用法：python -X utf8 -m benchmark.precise_fidelity
输出：benchmark/results/precise_fidelity_check.md + precise_fidelity_samples.jsonl
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark.api_cache import ApiCache
from benchmark.merge_fidelity import name_hit, content_bigrams

RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
N_SAMPLE = 60
# 论文 §6.2 的 verified 公式组件（λ=12 种子42）：主臂精回占比、批量占比、批量保真
MAIN_PRECISE, MAIN_BATCH, BATCH_FID_ID, BATCH_FID_TOPIC = 0.350, 0.650, 0.583, 0.596
GREEDY_COV = 0.694


def load_precise(arm):
    p = os.path.join(RES, f"decisions_merge_ovl_l12_drift_{arm}.jsonl")
    rows = [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
    return [d for d in rows if d.get("type") == "danmaku" and d.get("ev_id")]


def sample_even(items, n):
    if len(items) <= n:
        return items
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


async def main():
    from openai import AsyncOpenAI
    from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
    from persona import SYSTEM_PROMPT
    client = AsyncOpenAI(api_key=DEEPSEEK_API_KEY or "offline-cache-only", base_url=DEEPSEEK_BASE_URL)

    async def _gen(prompt: str) -> str:
        resp = await client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": prompt}],
            max_tokens=200, temperature=0.9,
            extra_body={"thinking": {"type": "disabled"}},
        )
        return (resp.choices[0].message.content or "").strip()

    cache = ApiCache("precise_gen", _gen, enabled=True)
    report = {}
    samples_out = []
    for arm in ("greedy", "full_merge_commit"):
        items = sample_even(load_precise(arm), N_SAMPLE)
        id_hits = topic_hits = topic_items = 0
        for d in items:
            reply = await cache(f"{d['username']}：{d['text']}")
            id_ok = name_hit(d["username"], reply)
            grams = content_bigrams(d["text"])
            tp_ok = bool(grams) and any(g in reply for g in grams)
            id_hits += id_ok
            if grams:
                topic_items += 1
                topic_hits += tp_ok
            samples_out.append({"arm": arm, "username": d["username"], "text": d["text"],
                                "reply": reply, "id_hit": bool(id_ok),
                                "topic_hit": bool(tp_ok) if grams else None})
        report[arm] = {"n": len(items), "id_hits": id_hits,
                       "f_id": id_hits / len(items),
                       "topic_items": topic_items, "topic_hits": topic_hits,
                       "f_topic": topic_hits / topic_items if topic_items else None}
        print(arm, report[arm], flush=True)
    print(cache.stats(), flush=True)

    with open(os.path.join(RES, "precise_fidelity_samples.jsonl"), "w", encoding="utf-8") as f:
        for s in samples_out:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")

    fg, fm = report["greedy"], report["full_merge_commit"]
    verified_main_id = MAIN_PRECISE * fm["f_id"] + MAIN_BATCH * BATCH_FID_ID
    verified_greedy_id = GREEDY_COV * fg["f_id"]
    verified_main_tp = MAIN_PRECISE * (fm["f_topic"] or 0) + MAIN_BATCH * BATCH_FID_TOPIC
    verified_greedy_tp = GREEDY_COV * (fg["f_topic"] or 0)

    lines = [
        "# 精确回复保真度 f 实测（红审 v3 洞 2，两臂对称口径）",
        "",
        "- 此前 verified = 35.0%×f + 65.0%×0.583 中 f=1.0 只是结构论证；审稿人指出 f=0.90 时卖点归零",
        "- 方法：λ=12_drift 种子42，greedy 与主臂各自精回消息等距抽 60 条，DeepSeek 真实逐条生成",
        "  （prompt = 生产管线同格式 `username：text` + persona 系统提示，温度 0.9，max_tokens 200；缓存 precise_gen）",
        "- 口径与 merge_fidelity 完全相同：ID 保真 = name_hit（精确子串/观众NN数字/≥3字用户名末2字后缀）；",
        "  话题保真 = 弹幕内容 bigram（CJK+数字，去停用字）任一命中；无 bigram 的弹幕不进话题分母",
        f"- 抽样：两臂各 {N_SAMPLE} 条（greedy 精回 260 条、主臂精回 126 条中等距抽样）；逐条样本见 precise_fidelity_samples.jsonl",
        "",
        "## 实测 f",
        "",
        "| 臂 | 样本 | ID 保真 f_id | 话题保真 f_topic（分母=有bigram条目） |",
        "|---|---|---|---|",
        f"| greedy | {fg['n']} | {fg['id_hits']}/{fg['n']} = **{fg['f_id']*100:.1f}%** | "
        f"{fg['topic_hits']}/{fg['topic_items']} = **{fg['f_topic']*100:.1f}%** |",
        f"| 主臂 full_merge_commit | {fm['n']} | {fm['id_hits']}/{fm['n']} = **{fm['f_id']*100:.1f}%** | "
        f"{fm['topic_hits']}/{fm['topic_items']} = **{fm['f_topic']*100:.1f}%** |",
        "",
        "## 对称修正后的 verified 对比（λ=12 种子42 组件）",
        "",
        "- 主臂 verified = 35.0%×f_main + 65.0%×0.583（ID 级；批量保真来自 merge_fidelity_check.md）",
        "- greedy verified = 69.4%×f_greedy（greedy 无批量，全部覆盖都是精回）",
        "",
        "| 口径 | greedy | 主臂 | 差 |",
        "|---|---|---|---|",
        f"| verified（ID 级） | {verified_greedy_id*100:.1f}% | {verified_main_id*100:.1f}% | "
        f"{(verified_main_id-verified_greedy_id)*100:+.1f}pp |",
        f"| verified（话题级，批量保真 0.596） | {verified_greedy_tp*100:.1f}% | {verified_main_tp*100:.1f}% | "
        f"{(verified_main_tp-verified_greedy_tp)*100:+.1f}pp |",
        "",
        "## 解读",
        "",
        "- 若两臂 f 都 ≈1：结构论证成立，verified 增益维持 +3.5pp，低分桶卖点不变",
        "- 若 f 显著 <1：按上表对称修正后重述头条；注意 f 对两臂同时打折，"
        "主臂只有 35% 组件受 f 影响，greedy 100% 组件受 f 影响——f<1 时差距反而**扩大**",
        "",
    ]
    out = os.path.join(RES, "precise_fidelity_check.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
