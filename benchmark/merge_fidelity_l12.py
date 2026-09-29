"""红审 v3：λ=12 批量保真补测 + 批级 Wilson 置信区间（含 λ=24 既有结果重算 CI）。

λ=12_drift 种子42 主臂（full_merge；变体臂在该负载下决策逐种子相同，见 bucket_breakdown.md），
批量 prompt 走真实 DeepSeek 生成（ApiCache merge_gen，与 merge_fidelity 同命名空间）。
同时按审稿人提醒：有效样本量 = 批次数（批内条目相关），给批级 Wilson 95% CI；
λ=24 既有 decisions_fidelity_* 文件不重跑，只重算批级 CI。

用法：python -X utf8 -m benchmark.merge_fidelity_l12
输出：benchmark/results/decisions_fidelity_ovl_l12_drift_full_merge.jsonl
      benchmark/results/merge_fidelity_l12_check.md
"""
import asyncio
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark.replay import (load_events_file, prescore_events, replay_scheduler,
                                write_jsonl, RESULTS_DIR, STUB_REPLY)
from benchmark.api_cache import ApiCache
from benchmark.merge_fidelity import batch_fidelity

SRC = "ovl_l12_drift"
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def wilson(k, n, z=1.96):
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (c - h, c + h)


def batch_level_ci(fids, key):
    """批级：把每批的命中率当二项分子分母合计过粗；按审稿人要求以批为有效样本量，
    报批均率 ± 正态近似 CI（n=批数），另附条目级 Wilson CI 并标注批内相关。"""
    rates = [f[key] for f in fids if f[key] is not None]
    n = len(rates)
    m = sum(rates) / n
    sd = (sum((r - m) ** 2 for r in rates) / (n - 1)) ** 0.5 if n > 1 else 0.0
    se = sd / math.sqrt(n)
    return m, n, (m - 1.96 * se, m + 1.96 * se)


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

    cache = ApiCache("merge_gen", _gen, enabled=True)
    events = load_events_file(os.path.join(DATA_DIR, f"{SRC}.jsonl"))
    scores = await prescore_events(events, ApiCache("score_danmaku", None, enabled=False))

    async def fidelity_reply(username, text):
        if username.startswith("（批量/"):
            return (await cache(text), "default_smile")
        return (STUB_REPLY, "default_smile")

    decisions, trace, sched = await replay_scheduler(
        events, scores, "fidelity_l12", "full", variant="full_merge",
        batch_merge=True, batch_q3_trigger=None, reply_fn=fidelity_reply)
    out = os.path.join(RESULTS_DIR, f"decisions_fidelity_{SRC}_full_merge.jsonl")
    write_jsonl(out, decisions)
    batches = [d for d in decisions if d["type"] == "batch"]
    fids = [batch_fidelity(d) for d in batches]
    print(f"λ=12 批量 {len(batches)} 次；{cache.stats()}", flush=True)

    tot_id_h = sum(f["id_hits"] for f in fids)
    tot_id_n = sum(f["n"] for f in fids)
    tot_tp_h = sum(f["topic_hits"] for f in fids)
    tot_tp_n = sum(f["topic_items"] for f in fids)
    m_id, n_b, ci_id = batch_level_ci(fids, "id_rate")
    m_tp, _, ci_tp = batch_level_ci(fids, "topic_rate")
    w_id = wilson(tot_id_h, tot_id_n)
    w_tp = wilson(tot_tp_h, tot_tp_n)

    lines = [
        "# λ=12 批量保真补测 + 批级置信区间（红审 v3）",
        "",
        f"- 流：`{SRC}` 种子42 主臂 full_merge（变体臂 λ=12 决策与其逐条相同，不重复测）",
        "- 方法同 merge_fidelity_check.md：批量 prompt 真实 DeepSeek 生成（merge_gen 缓存），ID/话题同口径",
        "- **有效样本量 = 批次数**（批内条目共享一次生成，相关不可忽略）：批级报均值±1.96SE；",
        "  条目级 Wilson 95% CI 一并给出但标注偏窄（把批内条目当独立）",
        "",
        f"## λ=12 主臂（{len(batches)} 批）",
        "",
        f"- ID 保真：批均 **{m_id*100:.1f}%**（n={n_b} 批，95% CI [{ci_id[0]*100:.1f}%, {ci_id[1]*100:.1f}%]）；"
        f"条目级 {tot_id_h}/{tot_id_n} = {tot_id_h/tot_id_n*100:.1f}%（Wilson [{w_id[0]*100:.1f}%, {w_id[1]*100:.1f}%]，偏窄）",
        f"- 话题保真：批均 **{m_tp*100:.1f}%**（95% CI [{ci_tp[0]*100:.1f}%, {ci_tp[1]*100:.1f}%]）；"
        f"条目级 {tot_tp_h}/{tot_tp_n} = {tot_tp_h/tot_tp_n*100:.1f}%（Wilson [{w_tp[0]*100:.1f}%, {w_tp[1]*100:.1f}%]，偏窄）",
        "",
        "## λ=24 既有结果重算批级 CI（数据：merge_fidelity_check.md 的 decisions_fidelity_*）",
        "",
        "| 臂 | 批数 | ID 批均 ± CI | 话题批均 ± CI |",
        "|---|---|---|---|",
    ]
    for arm, path in (("full_merge", "decisions_fidelity_ovl_l24_drift_full_merge.jsonl"),
                      ("full_merge_q3", "decisions_fidelity_ovl_l24_drift_full_merge_q3.jsonl")):
        ds = [json.loads(l) for l in open(os.path.join(RESULTS_DIR, path), encoding="utf-8") if l.strip()]
        f24 = [batch_fidelity(d) for d in ds if d["type"] == "batch"]
        mi, nb, ci_i = batch_level_ci(f24, "id_rate")
        mt, _, ci_t = batch_level_ci(f24, "topic_rate")
        lines.append(f"| {arm} | {nb} | {mi*100:.1f}% [{ci_i[0]*100:.1f}, {ci_i[1]*100:.1f}] | "
                     f"{mt*100:.1f}% [{ci_t[0]*100:.1f}, {ci_t[1]*100:.1f}] |")
    lines += [
        "",
        "## 结论",
        "",
        "- 正文保真数字（58.3%/59.6%）原为 λ=24 条目级；批级 CI 见上表，供 response letter 引用",
        "- λ=12 实测保真若与 λ=24 同量级，则 verified 公式中 0.583 的外推由'单点测量'升级为'双负载验证'",
        "",
    ]
    md = os.path.join(RESULTS_DIR, "merge_fidelity_l12_check.md")
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"→ {md}")


if __name__ == "__main__":
    asyncio.run(main())
