"""TCSS 评审 A 组 4：Regime Map——ρ 加密扫描下 策略差(full−greedy 评判均分) vs ρ。

λ ∈ {1.5, 3, 4.5, 6, 9, 12, 18, 24, 36} × {uniform, drift} × 种子 {42, 1001, 2002}。
流生成复刻 overload_gen.build_streams / merge_run.gen_stream：
  - 既有 λ 保留原 si 映射（l3=0,l6=1,l12=2,l24=3）与原时长（l3=60min，其余 30min），
    种子42 流与 benchmark/data/ 既有文件逐位一致（脚本断言验证）；
  - 新 λ 分配 si={l1p5:4, l4p5:5, l9:6, l18:7, l36:8}，时长 30min。
策略：fifo / greedy（单通道基线）+ full_commit（调度器无批量）。
指标：评判均分（被回弹幕）、总覆盖、占坑率（commits/enqueue_danmaku）、full−greedy 配对差。

用法：python -X utf8 -m benchmark.regime_map
输出：benchmark/results/regime_map.csv、regime_map.md；流文件 benchmark/data/regime_*.jsonl
"""
import asyncio
import json
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark.overload_gen import POOL_PATH, MIX_DRIFT, MIX_UNIFORM
from benchmark.synth_gen import GIFT_TABLE, USERS, REGULARS
from benchmark.replay import (load_events_file, prescore_events, replay_scheduler,
                                replay_baseline, write_jsonl)
from benchmark.metrics_ov import coverage_stats, stats_of
from benchmark.api_cache import ApiCache

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
RES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

# (key, λ/min, si, 时长min)；既有 λ 的 si/时长与原生成器一致
LAMBDAS = [("l1p5", 1.5, 4, 30), ("l3", 3.0, 0, 60), ("l4p5", 4.5, 5, 30),
           ("l6", 6.0, 1, 30), ("l9", 9.0, 6, 30), ("l12", 12.0, 2, 30),
           ("l18", 18.0, 7, 30), ("l24", 24.0, 3, 30), ("l36", 36.0, 8, 30)]
SEED_BASES = [42, 1001, 2002]
MU_NOMINAL = 6.7


def gen_stream(lk, lam, si, dur, profile, seed_base):
    pool = [json.loads(l) for l in open(POOL_PATH, encoding="utf-8")]
    buckets = {"high": [], "mid": [], "low": []}
    for r in pool:
        buckets[r["bucket"]].append(r["text"])
    gift_pool = []
    for g, p, w in GIFT_TABLE:
        gift_pool += [(g, p)] * w
    rng = random.Random(seed_base + si * 10 + (0 if profile == "uniform" else 1))
    name = f"regime_{lk}_{profile}"
    n = int(lam * dur)
    events, t = [], 0.0
    for i in range(n):
        t += rng.expovariate(lam / 60.0)
        seg = min(2, int(3 * i / n))
        mix = MIX_UNIFORM if profile == "uniform" else MIX_DRIFT[seg]
        bucket = rng.choices(list(mix), weights=list(mix.values()))[0]
        text = rng.choice(buckets[bucket])
        user = rng.choice(REGULARS) if rng.random() < 0.25 else rng.choice(USERS)
        events.append({"id": f"{name}-{i:05d}", "ts": round(t, 2), "type": "danmaku",
                       "username": user, "text": text, "gift_name": "", "num": 1,
                       "price": None, "recorded_score": None, "session": name, "tag": bucket})
        if rng.random() < 0.015:
            g, p = rng.choice(gift_pool)
            events.append({"id": f"{name}-g{i:05d}", "ts": round(t + 0.3, 2), "type": "gift",
                           "username": rng.choice(USERS + REGULARS), "text": "",
                           "gift_name": g, "num": 1, "price": p,
                           "recorded_score": None, "session": name})
    events.sort(key=lambda e: e["ts"])
    return events


def ensure_streams():
    paths = {}
    for lk, lam, si, dur in LAMBDAS:
        for profile in ("uniform", "drift"):
            for sb in SEED_BASES:
                name = f"regime_{lk}_{profile}" + ("" if sb == 42 else f"_s{sb}")
                path = os.path.join(DATA_DIR, f"{name}.jsonl")
                if not os.path.exists(path):
                    write_jsonl(path, gen_stream(lk, lam, si, dur, profile, sb))
                    print(f"  生成 {name}", flush=True)
                paths[(lk, profile, sb)] = path
    # 断言：既有 λ 种子42 流与原文件逐位一致
    for lk, orig in (("l3", "ovl_l3"), ("l6", "ovl_l6"), ("l12", "ovl_l12"), ("l24", "ovl_l24")):
        for profile in ("uniform", "drift"):
            mine = [json.loads(l) for l in open(paths[(lk, profile, 42)], encoding="utf-8")]
            ref = [json.loads(l) for l in open(os.path.join(DATA_DIR, f"{orig}_{profile}.jsonl"),
                                               encoding="utf-8")]
            strip = lambda rows: [{k: v for k, v in e.items() if k not in ("id", "session")}
                                  for e in rows]
            assert strip(mine) == strip(ref), f"{lk}_{profile} 种子42 复刻不一致"
    print("流就绪（4 条既有 λ × 2 profile 种子42 复刻验证通过）", flush=True)
    return paths


async def main():
    paths = ensure_streams()
    cache = ApiCache("score_danmaku", None, enabled=False)
    rows = []   # dict per (lk, profile, seed)
    for lk, lam, si, dur in LAMBDAS:
        for profile in ("uniform", "drift"):
            for sb in SEED_BASES:
                name = f"regime_{lk}_{profile}" + ("" if sb == 42 else f"_s{sb}")
                events = load_events_file(paths[(lk, profile, sb)])
                events_d = [json.loads(l) for l in open(paths[(lk, profile, sb)], encoding="utf-8")]
                scores = await prescore_events(events, cache)
                dec_g = await replay_baseline(events, scores, "greedy")
                dec_f = await replay_baseline(events, scores, "fifo")
                dec_full, trace, sched = await replay_scheduler(
                    events, scores, "regime", "full", variant="full_commit")
                for pol, dec in (("greedy", dec_g), ("fifo", dec_f), ("full_commit", dec_full)):
                    write_jsonl(os.path.join(RES, f"decisions_regime_{name}_{pol}.jsonl"), dec)
                sg, sf, su = stats_of(dec_g, events_d), stats_of(dec_f, events_d), stats_of(dec_full, events_d)
                commit_rate = sched.stats["commit"] / max(1, sched.stats["enqueue_danmaku"])
                rows.append({"lk": lk, "lam": lam, "rho": lam / MU_NOMINAL, "profile": profile,
                             "seed": sb,
                             "avg_greedy": sg["avg"], "avg_fifo": sf["avg"], "avg_full": su["avg"],
                             "diff_full_greedy": su["avg"] - sg["avg"],
                             "cov_greedy": sg["cov"], "cov_full": su["cov"], "cov_fifo": sf["cov"],
                             "commit_rate": commit_rate,
                             "n_dm": sg and sum(1 for e in events_d if e["type"] == "danmaku")})
                print(f"=== {name} 完成 ===", flush=True)
    print(cache.stats(), flush=True)

    import csv
    with open(os.path.join(RES, "regime_map.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    lines = ["# Regime Map：策略差 vs ρ（TCSS A4）",
             "",
             "- λ ∈ {1.5,3,4.5,6,9,12,18,24,36}（ρ=λ/6.7）× {uniform,drift} × 种子 {42,1001,2002}",
             "- 指标：full−greedy 评判均分配对差（3 种子 mean±SD）、覆盖率、占坑率",
             "- 既有 λ 种子42 流与预注册流逐位一致（断言通过）；l3 时长 60min 沿用原设定，其余 30min",
             "- 脚本 `benchmark/regime_map.py`，数据 regime_map.csv",
             "",
             "| λ | ρ | profile | full−greedy (mean±SD) | 逐种子差 | greedy 覆盖 | full 覆盖 | 占坑率 |",
             "|---|---|---|---|---|---|---|---|"]
    for lk, lam, si, dur in LAMBDAS:
        for profile in ("uniform", "drift"):
            sub = [r for r in rows if r["lk"] == lk and r["profile"] == profile]
            diffs = [r["diff_full_greedy"] for r in sub]
            lines.append(
                f"| {lam:g} | {lam/MU_NOMINAL:.2f} | {profile} | "
                f"{statistics.mean(diffs):+.2f} ± {statistics.stdev(diffs):.2f} | "
                f"{['%+.2f' % d for d in diffs]} | "
                f"{statistics.mean([r['cov_greedy'] for r in sub])*100:.1f}% | "
                f"{statistics.mean([r['cov_full'] for r in sub])*100:.1f}% | "
                f"{statistics.mean([r['commit_rate'] for r in sub])*100:.1f}% |")
    lines += ["", "## 读法", "",
              "- 差距显著不为零的区间 = full−greedy 的 3 种子均值带离开 0 的 ρ 段（预期只在欠载/临界附近）",
              "- 占坑率列即 63%→0.1% 曲线的加密版；与 occupancy_vs_load.csv 口径一致（commits/enqueue）",
              ""]
    out = os.path.join(RES, "regime_map.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"→ {out}")


if __name__ == "__main__":
    asyncio.run(main())
