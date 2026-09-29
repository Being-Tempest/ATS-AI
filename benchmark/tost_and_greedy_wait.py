"""终稿补充计算：TOST 等价性检验 + greedy 高分等待 5 种子。"""
import json, os
from scipy import stats
import math
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from benchmark.metrics_ov import coverage_stats, load_events

RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

# --- TOST：选择层 full_commit - greedy 配对差，5 种子 ---
# 逐种子差值（E1 seed_robustness.md / stats_verification.md: mean=-1.06 SD=0.336）
# 用 mean/SD 重建即可（TOST 只依赖汇总量），等价界 ±2 分
mean_d, sd_d, n, bound = -1.060, 0.336, 5, 2.0
se = sd_d / math.sqrt(n)
t1 = (mean_d - (-bound)) / se   # H0: mean <= -bound
t2 = (bound - mean_d) / se      # H0: mean >= +bound
df = n - 1
p1 = stats.t.sf(t1, df)
p2 = stats.t.sf(t2, df)
crit = stats.t.ppf(0.95, df)
ci90 = (mean_d - crit * se, mean_d + crit * se)
print(f"TOST selection: t1={t1:.2f} p1={p1:.4f} | t2={t2:.2f} p2={p2:.2e}")
print(f"  90% CI = [{ci90[0]:.2f}, {ci90[1]:.2f}] vs bounds ±{bound} -> "
      f"{'EQUIVALENT (both p<.05, CI inside)' if max(p1,p2)<0.05 else 'NOT equivalent'}")

# --- greedy 高分等待，λ=24_drift 五种子 ---
streams = ["ovl_l24_drift"] + [f"ovl_l24_drift_s{s}" for s in (1001, 2002, 3003, 4004)]
for arm in ("greedy",):
    waits = []
    for s in streams:
        dec = [json.loads(l) for l in open(os.path.join(RES, f"decisions_merge_{s}_{arm}.jsonl"), encoding="utf-8")]
        ev = load_events(s)
        st = coverage_stats(dec, ev)
        waits.append(st["high_wait"])
        print(f"{s} {arm}: high_wait={st['high_wait']:.1f}s high_precise={st['high_precise_rate']:.3f} cov={st['total_cov']:.3f}")
    m = sum(waits) / len(waits)
    sd = (sum((w - m) ** 2 for w in waits) / (len(waits) - 1)) ** 0.5
    print(f"{arm} high_wait 5-seed mean={m:.1f} SD={sd:.1f} range=[{min(waits):.0f},{max(waits):.0f}]")

# 顺带：主臂/变体臂 5 种子等待均值（核对正文用）
for arm in ("full_merge_commit", "full_merge_q3_commit"):
    waits = []
    for s in streams:
        dec = [json.loads(l) for l in open(os.path.join(RES, f"decisions_merge_{s}_{arm}.jsonl"), encoding="utf-8")]
        st = coverage_stats(dec, load_events(s))
        waits.append(st["high_wait"])
    m = sum(waits) / len(waits)
    sd = (sum((w - m) ** 2 for w in waits) / (len(waits) - 1)) ** 0.5
    print(f"{arm} high_wait 5-seed mean={m:.1f} SD={sd:.1f} values={[round(w) for w in waits]}")
