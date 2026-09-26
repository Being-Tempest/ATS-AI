"""
批量合并正式全量矩阵（红审4.5 §3，预注册 §8/§8.2）

策略：fifo / greedy / full_commit / full_merge_commit（主臂）/ full_merge_q3_commit（变体臂）
流：8 条既有流（λ={3,6,12,24}×{uniform,drift}）+ λ=12/24_drift 各 4 个新种子
    （种子基 {1001,2002,3003,4004}，生成机制复刻 overload_gen.build_streams，
     seed=42 已验证与预注册流逐位一致）
指标：三态覆盖 / 分桶覆盖 / 等待（高分桶单列）/ 高分精回率 / 礼物覆盖 / 点名保真率
     （仅 λ=24_drift 种子42 两臂真实生成）/ 成本列（LLM 调用、批量次数、平均批量、
     每槽位覆盖人数）
评分缓存全命中，零评分 API；批量占位 74 字 → 14.8s（§8.2.3 校准）。

用法：python -X utf8 -m benchmark.merge_run
输出：benchmark/results/decisions_merge_<stream>_<strategy>.jsonl
      benchmark/results/merge_summary.md
      benchmark/results/coverage_vs_load.csv
      benchmark/results/per_slot_coverage.csv
"""

import asyncio
import csv
import json
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark.overload_gen import POOL_PATH, LAMBDAS, DURATION_MIN, MIX_DRIFT, MIX_UNIFORM
from benchmark.synth_gen import GIFT_TABLE, USERS, REGULARS
from benchmark.replay import (load_events_file, prescore_events, replay_scheduler,
                                replay_baseline, write_jsonl, RESULTS_DIR)
from benchmark.metrics_ov import coverage_stats
from benchmark.merge_fidelity import batch_fidelity
from benchmark.api_cache import ApiCache

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
BASE_STREAMS = [f"ovl_{l}_{p}" for l in ("l3", "l6", "l12", "l24")
                for p in ("uniform", "drift")]
SEED_BASES = [1001, 2002, 3003, 4004]
SEED_STREAMS = {  # stream -> (lambda_key, si)：rng seed = base + si*10 + 1
    "ovl_l12_drift": ("l12", 2),
    "ovl_l24_drift": ("l24", 3),
}
STRATEGIES = ["fifo", "greedy", "full_commit", "full_merge_commit", "full_merge_q3_commit"]
LAM_NUM = {"l3": 3, "l6": 6, "l12": 12, "l24": 24}


def gen_stream(stream, seed_base):
    """复刻 build_streams 单流生成（drift/uniform 通用）；seed=42 应与既有文件逐位一致"""
    lk, si = SEED_STREAMS[stream]
    pool = [json.loads(l) for l in open(POOL_PATH, encoding="utf-8")]
    buckets = {"high": [], "mid": [], "low": []}
    for r in pool:
        buckets[r["bucket"]].append(r["text"])
    gift_pool = []
    for g, p, w in GIFT_TABLE:
        gift_pool += [(g, p)] * w
    lam = LAMBDAS[lk]
    rng = random.Random(seed_base + si * 10 + 1)
    n = int(lam * DURATION_MIN[lk])
    events, t = [], 0.0
    for i in range(n):
        t += rng.expovariate(lam / 60.0)
        seg = min(2, int(3 * i / n))
        mix = MIX_DRIFT[seg]
        bucket = rng.choices(list(mix), weights=list(mix.values()))[0]
        text = rng.choice(buckets[bucket]) if buckets[bucket] else "（空桶）"
        user = rng.choice(REGULARS) if rng.random() < 0.25 else rng.choice(USERS)
        events.append({"id": f"{stream}-{i:05d}", "ts": round(t, 2),
                       "type": "danmaku", "username": user, "text": text,
                       "gift_name": "", "num": 1, "price": None,
                       "recorded_score": None, "session": stream, "tag": bucket})
        if rng.random() < 0.015:
            g, p = rng.choice(gift_pool)
            events.append({"id": f"{stream}-g{i:05d}", "ts": round(t + 0.3, 2),
                           "type": "gift", "username": rng.choice(USERS + REGULARS),
                           "text": "", "gift_name": g, "num": 1, "price": p,
                           "recorded_score": None, "session": stream})
    events.sort(key=lambda e: e["ts"])
    return events


def ensure_seed_streams():
    for stream in SEED_STREAMS:
        orig = os.path.join(DATA_DIR, f"{stream}.jsonl")
        assert gen_stream(stream, 42) == [json.loads(l) for l in open(orig, encoding="utf-8")], \
            f"{stream} seed=42 复刻不一致"
        for sb in SEED_BASES:
            path = os.path.join(DATA_DIR, f"{stream}_s{sb}.jsonl")
            if not os.path.exists(path):
                write_jsonl(path, gen_stream(stream, sb))
                print(f"  生成 {path}", flush=True)
    print("种子流就绪（seed=42 复刻验证通过）", flush=True)


def cost_of(decisions, sched_stats=None):
    dm = [d for d in decisions if d["type"] == "danmaku"]
    batches = [d for d in decisions if d["type"] == "batch"]
    covered = sum(len(d.get("covered", [])) for d in batches)
    slots = len(decisions)
    responded = len(dm) + covered
    return {
        "llm_calls": slots,                      # 精回1次/条 + 批量1次/批
        "batch_n": len(batches),
        "batch_avg": covered / len(batches) if batches else 0.0,
        "per_slot_cov": responded / slots if slots else 0.0,
    }


async def run_one(stream, strategy, events, events_d, scores):
    if strategy in ("fifo", "greedy"):
        decisions = await replay_baseline(events, scores, strategy)
        stats = None
    else:
        q3 = 8 if strategy == "full_merge_q3_commit" else None
        bm = strategy in ("full_merge_commit", "full_merge_q3_commit")
        decisions, trace, sched = await replay_scheduler(
            events, scores, "merge", "full", variant=strategy,
            batch_merge=bm, batch_q3_trigger=q3)
        stats = sched.stats
    out = os.path.join(RESULTS_DIR, f"decisions_merge_{stream}_{strategy}.jsonl")
    write_jsonl(out, decisions)
    return decisions, stats


def fidelity_of(arm, stream):
    """λ=24_drift 种子42 的两条 merge 臂读 fidelity 决策文件算保真率，其余返回 None"""
    if stream != "ovl_l24_drift":
        return None
    path = os.path.join(RESULTS_DIR, f"decisions_fidelity_ovl_l24_drift_{arm}.jsonl")
    if not os.path.exists(path):
        return None
    fids = []
    for line in open(path, encoding="utf-8"):
        d = json.loads(line)
        if d.get("type") == "batch":
            fids.append(batch_fidelity(d))
    id_h = sum(f["id_hits"] for f in fids)
    id_n = sum(f["n"] for f in fids)
    tp_h = sum(f["topic_hits"] for f in fids)
    tp_n = sum(f["topic_items"] for f in fids)
    return {"id": id_h / id_n if id_n else None, "topic": tp_h / tp_n if tp_n else None}


async def main():
    ensure_seed_streams()
    cache = ApiCache("score_danmaku", None, enabled=False)

    all_streams = BASE_STREAMS + [f"{s}_s{sb}" for s in SEED_STREAMS for sb in SEED_BASES]
    table = {}   # (stream, strategy) -> {cov..., cost...}
    for stream in all_streams:
        base = stream.split("_s")[0]
        events = load_events_file(os.path.join(DATA_DIR, f"{stream}.jsonl"))
        events_d = [json.loads(l) for l in open(os.path.join(DATA_DIR, f"{stream}.jsonl"),
                                                encoding="utf-8")]
        scores = await prescore_events(events, cache)
        for st in STRATEGIES:
            decisions, stats = await run_one(stream, st, events, events_d, scores)
            c = coverage_stats(decisions, events_d)
            c.update(cost_of(decisions, stats))
            table[(stream, st)] = c
        print(f"=== {stream} 完成 ===", flush=True)
    print(cache.stats(), flush=True)

    # ── fidelity（λ=24_drift 种子42 两臂）──
    fid = {"full_merge_commit": fidelity_of("full_merge", "ovl_l24_drift"),
           "full_merge_q3_commit": fidelity_of("full_merge_q3", "ovl_l24_drift")}

    # ── merge_summary.md ──
    lines = [
        "# 批量合并正式对比（预注册 §8/§8.2，红审4.5 §3）",
        "",
        "- 主臂 full_merge_commit（Q3 级联门控，忠于 roadmap）/ 变体臂 full_merge_q3_commit"
        "（Q3-D≥8 积压触发，每批 ≤10）",
        "- 批量占位 74 字 → 14.8s/批（§8.2.3 校准）；确定性重放，报配对差不报 CI",
        "- 点名保真率仅 λ=24_drift 种子42 两臂为真实生成（merge_fidelity_check.md），其余 —",
        "",
    ]

    def row(stream, st):
        c = table[(stream, st)]
        t = c["tiers"]
        f = fid.get(st) if stream == "ovl_l24_drift" else None
        fid_s = f"{f['id']*100:.0f}%/{f['topic']*100:.0f}%" if f and f["id"] is not None else "—"
        return (f"| {st} | {c['total_cov']*100:.1f}% | {c['precise_rate']*100:.1f}% | "
                f"{c['batch_cov_rate']*100:.1f}% | {t['low']['cov']*100:.1f}% | "
                f"{t['mid']['cov']*100:.1f}% | {t['high']['cov']*100:.1f}% | "
                f"{c['high_precise_rate']*100:.1f}% | {c['high_wait']:.0f} | "
                f"{c['gift_cov']*100:.1f}% | {fid_s} | {c['llm_calls']} | {c['batch_n']} | "
                f"{c['batch_avg']:.1f} | {c['per_slot_cov']:.2f} |")

    HDR = ("| 策略 | 总覆盖 | 精回 | 批量覆盖 | 低分桶 | 中分桶 | 高分桶 | 高分精回 | "
           "高分等待(s) | 礼物覆盖 | 保真ID/话题 | LLM调用 | 批量 | 均批量 | 每槽覆盖 |")
    SEP = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"

    for stream in BASE_STREAMS:
        lk = stream.split("_")[1]
        lam = LAM_NUM[lk]
        profile = "drift" if stream.endswith("drift") else "uniform"
        lines += [f"## `{stream}`（λ={lam}/min ≈ {lam/6.7:.1f}μ，{profile}）", "", HDR, SEP]
        for st in STRATEGIES:
            lines.append(row(stream, st))
        # 配对差
        g = table[(stream, "greedy")]
        for arm in ("full_merge_commit", "full_merge_q3_commit"):
            m = table[(stream, arm)]
            lines.append(
                f"- {arm} − greedy：总覆盖 {(m['total_cov']-g['total_cov'])*100:+.1f}pp，"
                f"低分桶 {(m['tiers']['low']['cov']-g['tiers']['low']['cov'])*100:+.1f}pp，"
                f"高分精回 {(m['high_precise_rate']-g['high_precise_rate'])*100:+.1f}pp，"
                f"高分等待 {m['high_wait']-g['high_wait']:+.0f}s")
        lines.append("")

    # ── 5 种子一致性（λ=12/24_drift）──
    lines += ["## 种子稳健性（5 种子：42 原始 + 1001/2002/3003/4004）", ""]
    for stream in SEED_STREAMS:
        streams5 = [stream] + [f"{stream}_s{sb}" for sb in SEED_BASES]
        lines += [f"### `{stream}`", "",
                  "| 种子 | 臂 | 总覆盖 | vs greedy | 低分桶 vs greedy | 高分精回 | 高分等待(s) |",
                  "|---|---|---|---|---|---|---|"]
        for arm in ("full_merge_commit", "full_merge_q3_commit"):
            diffs, low_diffs = [], []
            for s in streams5:
                m, g = table[(s, arm)], table[(s, "greedy")]
                d = (m["total_cov"] - g["total_cov"]) * 100
                dl = (m["tiers"]["low"]["cov"] - g["tiers"]["low"]["cov"]) * 100
                diffs.append(d)
                low_diffs.append(dl)
                lines.append(
                    f"| {s.split('_s')[-1] if '_s' in s else '42（原）'} | {arm} | "
                    f"{m['total_cov']*100:.1f}% | {d:+.1f}pp | {dl:+.1f}pp | "
                    f"{m['high_precise_rate']*100:.1f}% | {m['high_wait']:.0f} |")
            n_pos = sum(1 for d in diffs if d >= 8)
            lines += [
                f"- {arm}：总覆盖差 5 种子均值 **{statistics.mean(diffs):+.1f}pp** "
                f"± {statistics.stdev(diffs):.1f}，范围 [{min(diffs):+.1f}, {max(diffs):+.1f}]，"
                f"≥+8pp 的种子 {n_pos}/5"
                + ("；**有反向/不足种子，claim 需降级**" if n_pos < 5 else "，5/5 同向达标"),
                "",
            ]

    out = os.path.join(RESULTS_DIR, "merge_summary.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"→ {out}", flush=True)

    # ── coverage_vs_load.csv ──
    with open(os.path.join(RESULTS_DIR, "coverage_vs_load.csv"), "w", newline="",
              encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stream", "lambda_per_min", "profile", "seed", "strategy",
                    "total_cov", "precise_rate", "batch_cov_rate", "ignore_rate",
                    "low_cov", "mid_cov", "high_cov", "high_precise_rate",
                    "high_wait_s", "mean_wait_s", "gift_cov"])
        for stream in all_streams:
            base = stream.split("_s")[0]
            lk = base.split("_")[1]
            seed = stream.split("_s")[1] if "_s" in stream else "42"
            profile = "drift" if base.endswith("drift") else "uniform"
            for st in STRATEGIES:
                c = table[(stream, st)]
                w.writerow([stream, LAM_NUM[lk], profile, seed, st,
                            f"{c['total_cov']:.4f}", f"{c['precise_rate']:.4f}",
                            f"{c['batch_cov_rate']:.4f}", f"{c['ignore_rate']:.4f}",
                            f"{c['tiers']['low']['cov']:.4f}", f"{c['tiers']['mid']['cov']:.4f}",
                            f"{c['tiers']['high']['cov']:.4f}", f"{c['high_precise_rate']:.4f}",
                            f"{c['high_wait']:.1f}", f"{c['mean_wait']:.1f}",
                            f"{c['gift_cov']:.4f}"])
    print("→ coverage_vs_load.csv", flush=True)

    # ── per_slot_coverage.csv ──
    with open(os.path.join(RESULTS_DIR, "per_slot_coverage.csv"), "w", newline="",
              encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["stream", "strategy", "llm_calls", "batch_n", "batch_avg_size",
                    "slots", "responded_dm", "per_slot_coverage"])
        for stream in all_streams:
            for st in STRATEGIES:
                c = table[(stream, st)]
                w.writerow([stream, st, c["llm_calls"], c["batch_n"],
                            f"{c['batch_avg']:.2f}", c["llm_calls"],
                            round(c["per_slot_cov"] * c["llm_calls"]),
                            f"{c['per_slot_cov']:.4f}"])
    print("→ per_slot_coverage.csv", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
