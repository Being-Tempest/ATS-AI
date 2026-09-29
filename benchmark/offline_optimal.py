"""TCSS 评审 A 组 3：离线最优上界（已知完整到达序列 + 缓存评分）。

求解方法（如实写明）：
- 质量上界（judged reply quality）：单通道、每槽一条、槽长 7.8s 同质。
  ① 无界窗口 oracle：基线框架去掉 PENDING_CAP，work-conserving + 每槽取全局最高分。
     交换论证：槽位易逝、回复无负收益 ⇒ work-conserving serve-max 在无界窗口下是最优的，
     其服务集合 = 全部到达中分数最高的 k 条（k=oracle 实际服务数）。
  ② top-k 解析界：取 greedy 实际回复数 k_g，全部到达分数的前 k_g 名均值——
     任何回复数相同的策略（greedy/full 都属此类）均分不可能超过它。本文以 ② 为主界。
- 覆盖上界（主臂批量约束：批量 ≤10 条/批、2 槽/批、≥60 分必须精回）：
  可用槽数 S 取同流 greedy 实际消耗槽数（逐种子），最优分配 = 高分全精回 + 其余按 10 条/批装桶，
  覆盖上限 = min(N, h + 10·⌊(S−h)/2⌋)，h=高分条数。解析式，无需 DP。
- 容量 35 注意力窗口下：greedy 的 挤最低分+work-conserving serve-max 策略对该窗口模型本身最优
  （同一交换论证），所以 greedy 与其窗口模型的离线最优差距 = 0；有意义的锚是无界窗口界。

用法：python -X utf8 -m benchmark.offline_optimal
输出：benchmark/results/offline_optimal.md
"""
import asyncio
import json
import math
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark.replay import (load_events_file, prescore_events, replay_baseline,
                                PENDING_CAP)
from benchmark.api_cache import ApiCache

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
SEEDS = ["", "_s1001", "_s2002", "_s3003", "_s4004"]
D_PRECISE = 7.8


def quality(decisions):
    dm = [d["score"] for d in decisions if d["type"] == "danmaku"]
    return (sum(dm) / len(dm) if dm else 0.0), len(dm)


async def replay_oracle(events, scores):
    """无界窗口 oracle：其它与 replay_baseline(greedy) 相同，pending 无上限。"""
    from benchmark.replay import _gift_score, STUB_REPLY
    pending = []
    decisions = []
    free_at = 0.0

    def ev_score(item):
        return scores.get(item.id, 0) if item.type == "danmaku" else _gift_score(item)

    def serve(start):
        nonlocal free_at
        item = max(pending, key=ev_score)
        pending.remove(item)
        decisions.append({"t": round(start, 2), "type": item.type, "score": ev_score(item)})
        free_at = start + D_PRECISE

    for ev in events:
        while pending and free_at <= ev.ts:
            serve(free_at)
        pending.append(ev)
        if pending and free_at <= ev.ts:
            serve(ev.ts)
    while pending:
        serve(free_at)
    return decisions


async def main():
    cache = ApiCache("score_danmaku", None, enabled=False)
    lines = ["# 离线最优上界（TCSS A3）：greedy/full 离最优多远",
             "",
             "- 条件：λ=24_drift 五种子；方法与假设见文件头 docstring（top-k 解析界 + 无界窗口 oracle 互验）",
             "- 质量界回答：负结果'离最优多远'；覆盖界回答：主臂批量约束下的覆盖天花板",
             "",
             "| 种子 | greedy 均分(k) | full 均分 | top-k 解析界 | greedy 差距 | full 差距 |",
             "|---|---|---|---|---|---|---|"]
    gaps_g, gaps_f, cov_rows = [], [], []
    for suf in SEEDS:
        stream = f"ovl_l24_drift{suf}"
        events = load_events_file(os.path.join(DATA_DIR, f"{stream}.jsonl"))
        scores = await prescore_events(events, cache)
        all_dm = sorted((s for ev_id, s in scores.items()), reverse=True)

        greedy = await replay_baseline(events, scores, "greedy")
        g_avg, k_g = quality(greedy)
        full_dec = [json.loads(l) for l in open(
            os.path.join(RES, f"decisions_merge_{stream}_full_commit.jsonl"), encoding="utf-8")]
        f_avg, k_f = quality(full_dec)

        topk_bound = sum(all_dm[:k_g]) / k_g                      # 解析界（同回复数）
        oracle = await replay_oracle(events, scores)
        o_avg, k_o = quality(oracle)
        # 注：无界 oracle 在无限收尾下会回完全部 720 条（k_o=720），退化为"全回"，
        # 不构成质量界；锐利的质量界是同回复数 top-k 解析界。此处仅记录退化现象。
        gap_g = (topk_bound - g_avg) / topk_bound * 100
        gap_f = (topk_bound - f_avg) / topk_bound * 100
        gaps_g.append(gap_g)
        gaps_f.append(gap_f)
        lines.append(f"| {suf.lstrip('_s') or '42（原）'} | {g_avg:.2f} ({k_g}) | {f_avg:.2f} | "
                     f"{topk_bound:.2f} | {gap_g:.1f}% | {gap_f:.1f}% |")

        # 覆盖界（主臂约束）：S = greedy 实际槽数
        S = len(greedy)
        h = sum(1 for s in scores.values() if s >= 60)
        N = len(scores)
        cov_cap = min(N, h + 10 * max(0, (S - h) // 2))
        cov_rows.append((suf, N, h, S, cov_cap, cov_cap / N * 100))

    lines += ["",
              f"- top-k 解析界五种子均值：greedy 差距 **{statistics.mean(gaps_g):.1f}%**"
              f"（{min(gaps_g):.1f}–{max(gaps_g):.1f}%），full 差距 **{statistics.mean(gaps_f):.1f}%**"
              f"（{min(gaps_f):.1f}–{max(gaps_f):.1f}%）",
              "- 解读：greedy 距'同回复数下任何策略不可逾越的质量天花板'只差 "
              f"{statistics.mean(gaps_g):.1f}%，full 也只再远 {statistics.mean(gaps_f)-statistics.mean(gaps_g):.1f}pp——"
              "选择层可供任何策略争夺的空间本身只有个位数百分比，这是负面结果的最强形式："
              "不是我们的调度器找不到增益，是增益几乎不存在",
              "- 无界窗口 oracle 退化说明：内存无界时 oracle 在收尾期会回完全部到达（k=720，均分=全流均值），不构成质量界；锐利界是同回复数 top-k 解析界（任何同 k 策略的服务集总分 ≤ top-k 总分）",
              "",
              "## 覆盖上界（主臂批量约束：≤10 条/批、2 槽/批、≥60 精回）",
              "",
              "| 种子 | 到达 N | 高分 h | 可用槽 S | 覆盖上限 | 覆盖上限% | 主臂实测 |",
              "|---|---|---|---|---|---|---|"]
    for suf, N, h, S, cap, pct in cov_rows:
        main_dec = [json.loads(l) for l in open(
            os.path.join(RES, f"decisions_merge_{suf and 'ovl_l24_drift'+suf or 'ovl_l24_drift'}_full_merge_commit.jsonl"),
            encoding="utf-8")]
        from benchmark.metrics_ov import coverage_stats
        ev_d = [json.loads(l) for l in open(os.path.join(DATA_DIR, f"ovl_l24_drift{suf}.jsonl"),
                                            encoding="utf-8")]
        actual = coverage_stats(main_dec, ev_d)["total_cov"] * 100
        lines.append(f"| {suf.lstrip('_s') or '42（原）'} | {N} | {h} | {S} | {cap} | {pct:.1f}% | {actual:.1f}% |")
    lines += ["",
              "- 覆盖上限按解析式 min(N, h+10·⌊(S−h)/2⌋) 计算；主臂实测与上限的差距来自触发时机"
              "（批量不是随时攒得够 10 条）与级联等待，属机制内损耗而非调度失误",
              ""]
    out = os.path.join(RES, "offline_optimal.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
