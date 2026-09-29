"""红审 v3 数据任务（离线部分）：λ=24 聚合层 t 检验 + 逐桶拆分 + λ=12 高分等待。

用法：python -X utf8 -m benchmark.redreview_v3_stats
输出：stdout 汇总（结果誊入 stats_verification.md / bucket_breakdown.md / merge_summary.md）
"""
import json
import math
import os
import sys

from scipy import stats

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from benchmark.metrics_ov import coverage_stats, load_events

RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
SEEDS = ["", "_s1001", "_s2002", "_s3003", "_s4004"]


def dec(stream, arm):
    p = os.path.join(RES, f"decisions_merge_{stream}_{arm}.jsonl")
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def ttest(gains, label, mu0=0.0):
    n = len(gains)
    m = sum(gains) / n
    sd = (sum((g - m) ** 2 for g in gains) / (n - 1)) ** 0.5
    se = sd / math.sqrt(n)
    t = (m - mu0) / se
    p_two = 2 * stats.t.sf(abs(t), n - 1)
    crit = stats.t.ppf(0.975, n - 1)
    ci = (m - crit * se, m + crit * se)
    print(f"{label}: gains={['%+.1f' % g for g in gains]}")
    print(f"  mean={m:+.2f} SD={sd:.2f} | t({n-1})={t:.2f} p={p_two:.2e} "
          f"95% CI [{ci[0]:+.2f}, {ci[1]:+.2f}] (vs {mu0})")
    return m, sd, se


print("=== 1. λ=24 drift 聚合层配对增益 t 检验（5 种子） ===")
for arm, name in (("full_merge_commit", "主臂"), ("full_merge_q3_commit", "变体臂")):
    gains = []
    for s in SEEDS:
        stream = f"ovl_l24_drift{s}"
        ev = load_events(stream)
        cg = coverage_stats(dec(stream, "greedy"), ev)["total_cov"]
        ca = coverage_stats(dec(stream, arm), ev)["total_cov"]
        gains.append((ca - cg) * 100)
    m, sd, se = ttest(gains, f"{arm} ({name}) − greedy")
    # 针对幅度判据 ≥8pp 的单侧检验：H0: 平均增益 ≥ 8, H1: < 8
    t8 = (m - 8.0) / se
    p8 = stats.t.cdf(t8, 4)
    print(f"  幅度判据检验（H0: 增益≥8pp, 单侧）: t(4)={t8:.2f}, p={p8:.4f} -> "
          f"{'无法拒绝≥8' if p8 > 0.05 else '显著低于 8pp'}")
    # 增益的 90% 置信上界（单侧 95% 上界）
    up90 = m + stats.t.ppf(0.90, 4) * se
    up95 = m + stats.t.ppf(0.95, 4) * se
    print(f"  单侧置信上界: 90% 上界 {up90:+.2f}pp, 95% 上界 {up95:+.2f}pp")

print()
print("=== 4. λ=12 drift 高分等待（5 种子，秒） ===")
for arm in ("greedy", "full_merge_commit", "full_merge_q3_commit"):
    waits = []
    for s in SEEDS:
        stream = f"ovl_l12_drift{s}"
        st = coverage_stats(dec(stream, arm), load_events(stream))
        waits.append(st["high_wait"])
    m = sum(waits) / len(waits)
    sd = (sum((w - m) ** 2 for w in waits) / (len(waits) - 1)) ** 0.5
    print(f"{arm}: mean={m:.1f}s SD={sd:.1f} values={[round(w) for w in waits]}")

print()
print("=== 2. 逐桶拆分（到达/精回/批量覆盖/丢弃=未服务） ===")
for lam in ("l12", "l24"):
    for arm in ("greedy", "full_merge_commit", "full_merge_q3_commit"):
        agg = {b: [0, 0, 0] for b in ("high", "mid", "low")}
        for s in SEEDS:
            stream = f"ovl_{lam}_drift{s}"
            st = coverage_stats(dec(stream, arm), load_events(stream))
            for b in ("high", "mid", "low"):
                t = st["tiers"][b]
                agg[b][0] += t["precise"]
                agg[b][1] += t["batch"]
                agg[b][2] += t["ignored"]
        print(f"ovl_{lam}_drift 5 种子合计 / {arm}:")
        print("  桶 | 到达 | 精回 | 批量覆盖 | 未服务 | 覆盖%")
        for b in ("high", "mid", "low"):
            p, bt, ig = agg[b]
            n = p + bt + ig
            print(f"  {b} | {n} | {p} | {bt} | {ig} | {(p+bt)/n*100:.1f}%")
        # 种子 42 单流
        st = coverage_stats(dec(f"ovl_{lam}_drift", arm), load_events(f"ovl_{lam}_drift"))
        print(f"  （种子42 单流）")
        for b in ("high", "mid", "low"):
            t = st["tiers"][b]
            print(f"  {b} | {t['n']} | {t['precise']} | {t['batch']} | {t['ignored']} | {t['cov']*100:.1f}%")
