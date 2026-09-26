"""
过载实验分析（E1-ovl / E4-ovl）— 配对差值 + 效应量 + 分段（灌水期）指标

用法：python -m benchmark.metrics_ov
输入：results/decisions_ovl_*.jsonl, results/run_meta.jsonl, benchmark/data/ovl_*.jsonl
输出：
  results/ovl_summary.md          主对比表（含灌水期饿死率、配对差值、效应量）
  results/divergence_cases.jsonl  full vs greedy 分歧决策样本（追加全部流）
  results/occupancy_vs_load.csv   占坑触发率 vs 负载
"""

import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")

STREAMS = [f"ovl_{l}_{p}" for l in ("l3", "l6", "l12", "l24")
           for p in ("uniform", "drift")]
LAMBDA = {"l3": 3, "l6": 6, "l12": 12, "l24": 24}
BASELINES = ["fifo", "random", "keyword", "greedy"]
SCHED = ["fixed50_commit", "fixed50_nocommit", "full_commit", "full_nocommit"]


def load_dec(src, st):
    p = os.path.join(RESULTS_DIR, f"decisions_ovl_{src}_{st}.jsonl")
    if not os.path.exists(p):
        return None
    return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]


def load_events(src):
    return [json.loads(l) for l in open(os.path.join(DATA_DIR, f"{src}.jsonl"),
                                       encoding="utf-8") if l.strip()]


def stats_of(decisions, events):
    total_dm = sum(1 for e in events if e["type"] == "danmaku")
    total_gf = sum(1 for e in events if e["type"] in ("gift", "sc"))
    dm = [d for d in decisions if d["type"] == "danmaku"]
    gf = [d for d in decisions if d["type"] in ("gift", "sc")]
    scores = [d["score"] for d in dm]
    # 分段（灌水期 = 中 1/3，按事件时间等分）
    if events:
        t0, t1 = events[0]["ts"], events[-1]["ts"]
        seg_lo, seg_hi = t0 + (t1 - t0) / 3, t0 + 2 * (t1 - t0) / 3
    else:
        seg_lo = seg_hi = 0
    mid_arr = [e for e in events if e["type"] == "danmaku" and seg_lo <= e["ts"] < seg_hi]
    mid_rep = [d for d in dm if seg_lo <= d["t"] < seg_hi]
    return {
        "replies": len(decisions), "dm": len(dm),
        "cov": len(dm) / total_dm if total_dm else 0,
        "avg": sum(scores) / len(scores) if scores else 0.0,
        "scores": scores,
        "gift_miss": 1 - len(gf) / total_gf if total_gf else 0.0,
        "mid_cov": len(mid_rep) / len(mid_arr) if mid_arr else 0.0,
        "mid_avg": (sum(d["score"] for d in mid_rep) / len(mid_rep)) if mid_rep else 0.0,
    }


def coverage_stats(decisions, events):
    """三态覆盖统计（批量合并实验口径，预注册 §8）：
    每条事件最终状态 = 精回（单独回复）/ 批量覆盖（被某次批量回应 drain）/ 无视。
    依赖 decisions 里的 ev_id / arrive_ts / covered（新版回放器输出）。
    分桶用事件自带 tag（ovl 流为实测桶 high/mid/low），缺 tag 时按决策分推定。"""
    dm_events = {e["id"]: e for e in events if e["type"] == "danmaku"}
    gf_events = {e["id"]: e for e in events if e["type"] in ("gift", "sc")}
    precise, covered = {}, {}     # ev_id -> 回应时刻 t
    n_batch = 0
    for d in decisions:
        if d.get("type") == "batch":
            n_batch += 1
            for c in d.get("covered", []):
                if c.get("ev_id") and c["ev_id"] not in precise:
                    covered[c["ev_id"]] = d["t"]
        elif d.get("ev_id"):
            precise[d["ev_id"]] = d["t"]
            covered.pop(d["ev_id"], None)   # 精回优先于批量覆盖

    def bucket_of(ev_id, score=None):
        ev = dm_events.get(ev_id)
        if ev and ev.get("tag"):
            return ev["tag"]
        s = score if score is not None else 0
        return "high" if s >= 60 else ("mid" if s >= 30 else "low")

    # 每条弹幕的分数（events 里没存分，从 decisions 反查；未回应的用 tag 即可）
    dm_ids = list(dm_events)
    tiers = {"high": [0, 0, 0], "mid": [0, 0, 0], "low": [0, 0, 0]}  # [精回, 批量覆盖, 无视]
    waits = []
    tier_waits = {"high": [], "mid": [], "low": []}
    high_total = high_precise = 0
    for ev_id in dm_ids:
        b = bucket_of(ev_id)
        if ev_id in precise:
            tiers[b][0] += 1
            if dm_events[ev_id].get("ts") is not None:
                w = precise[ev_id] - dm_events[ev_id]["ts"]
                waits.append(w)
                tier_waits[b].append(w)
        elif ev_id in covered:
            tiers[b][1] += 1
            if dm_events[ev_id].get("ts") is not None:
                w = covered[ev_id] - dm_events[ev_id]["ts"]
                waits.append(w)
                tier_waits[b].append(w)
        else:
            tiers[b][2] += 1
        if b == "high":
            high_total += 1
            if ev_id in precise:
                high_precise += 1
    n_dm = len(dm_ids)
    n_precise = sum(t[0] for t in tiers.values())
    n_cov = sum(t[1] for t in tiers.values())
    gf_replied = sum(1 for i in gf_events if i in precise or i in covered)
    return {
        "batch_events": n_batch,
        "total_cov": (n_precise + n_cov) / n_dm if n_dm else 0.0,
        "precise_rate": n_precise / n_dm if n_dm else 0.0,
        "batch_cov_rate": n_cov / n_dm if n_dm else 0.0,
        "ignore_rate": 1 - (n_precise + n_cov) / n_dm if n_dm else 0.0,
        "tiers": {b: {"n": sum(t), "precise": t[0], "batch": t[1], "ignored": t[2],
                      "cov": (t[0] + t[1]) / sum(t) if sum(t) else 0.0,
                      "mean_wait": (sum(tier_waits[b]) / len(tier_waits[b])
                                    if tier_waits[b] else 0.0)}
                  for b, t in tiers.items()},
        "mean_wait": sum(waits) / len(waits) if waits else 0.0,
        "high_wait": (sum(tier_waits["high"]) / len(tier_waits["high"])
                      if tier_waits["high"] else 0.0),
        "high_precise_rate": high_precise / high_total if high_total else 0.0,
        "gift_cov": gf_replied / len(gf_events) if gf_events else 0.0,
    }


def effect_size(a, b):
    """Cohen's d：a 相对 b 的均分差 / 合并 SD"""
    if len(a) < 2 or len(b) < 2:
        return 0.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    sp = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / (len(a) + len(b) - 2))
    return (ma - mb) / sp if sp else 0.0


def main():
    all_events = {s: load_events(s) for s in STREAMS
                  if os.path.exists(os.path.join(DATA_DIR, f"{s}.jsonl"))}
    strategies = BASELINES + SCHED
    table = {}   # (src, st) -> stats
    decisions_cache = {}
    for src in all_events:
        for st in strategies:
            d = load_dec(src, st)
            if d is not None:
                table[(src, st)] = stats_of(d, all_events[src])
                decisions_cache[(src, st)] = d

    lines = ["# 过载+质量漂移调度对比（E1-ovl，预注册 preregistration.md）", ""]
    lines.append("> 确定性重放：共享评分缓存 + 固定种子，策略间差异无采样随机性。"
                 "对比报配对差值与效应量（Cohen's d），不报 CI。"
                 "占位回复 9.0s/条（μ≈6.7 条/分钟，E3 实测口径）。"
                 "合成数据，与真实流分开。")
    lines.append("")
    lines.append("分段口径：灌水期 = 按事件时间等分的中 1/3 段；灌水期回复率 = 段内回复/段内到达。")
    lines.append("")

    for src in all_events:
        lam = LAMBDA[src.split("_")[1]]
        drift = "漂移" if src.endswith("drift") else "均匀"
        ev = all_events[src]
        ndm = sum(1 for e in ev if e["type"] == "danmaku")
        lines.append(f"## `{src}`（λ={lam}/min ≈ {lam/6.7:.1f}μ，{drift}质量，弹幕 {ndm}）")
        lines.append("")
        lines.append("| 策略 | 回复数 | 回复率 | 均分 | 灌水期回复率 | 灌水期均分 | 礼物漏回 |")
        lines.append("|---|---|---|---|---|---|---|")
        for st in strategies:
            c = table.get((src, st))
            if not c:
                continue
            lines.append(f"| {st} | {c['replies']} | {c['cov']*100:.1f}% | {c['avg']:.1f} | "
                         f"{c['mid_cov']*100:.1f}% | {c['mid_avg']:.1f} | "
                         f"{c['gift_miss']*100:.1f}% |")
        # 配对差值 + 效应量（full vs greedy / fixed，commit 臂为线上配置）
        lines.append("")
        for a, b in (("full_commit", "greedy"), ("full_commit", "fixed50_commit"),
                     ("full_nocommit", "greedy")):
            ca, cb = table.get((src, a)), table.get((src, b))
            if ca and cb:
                d = effect_size(ca["scores"], cb["scores"])
                lines.append(f"- {a} − {b}：均分差 {ca['avg']-cb['avg']:+.1f}，"
                             f"效应量 d={d:+.2f}，回复率差 {(ca['cov']-cb['cov'])*100:+.1f}pp")
        lines.append("")

    out = os.path.join(RESULTS_DIR, "ovl_summary.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"→ {out}")

    # ── full vs greedy 分歧样本 ──
    div_path = os.path.join(RESULTS_DIR, "divergence_cases.jsonl")
    n_div = 0
    with open(div_path, "a", encoding="utf-8") as f:
        for src in all_events:
            fa = decisions_cache.get((src, "full_commit"))
            fb = decisions_cache.get((src, "greedy"))
            if not fa or not fb:
                continue
            sa = {(d["username"], d["text"]) for d in fa}
            sb = {(d["username"], d["text"]) for d in fb}
            ja = len(sa & sb) / len(sa | sb) if sa | sb else 1.0
            for d in fa:
                if (d["username"], d["text"]) not in sb:
                    f.write(json.dumps({"src": src, "only_in": "full_commit",
                                        "jaccard": round(ja, 3), **d},
                                       ensure_ascii=False) + "\n")
                    n_div += 1
            for d in fb:
                if (d["username"], d["text"]) not in sa:
                    f.write(json.dumps({"src": src, "only_in": "greedy",
                                        "jaccard": round(ja, 3), **d},
                                       ensure_ascii=False) + "\n")
                    n_div += 1
    print(f"→ {div_path}（追加 {n_div} 条分歧样本）")

    # ── 占坑率 vs 负载 ──
    occ_path = os.path.join(RESULTS_DIR, "occupancy_vs_load.csv")
    metas = []
    mp = os.path.join(RESULTS_DIR, "run_meta.jsonl")
    if os.path.exists(mp):
        for l in open(mp, encoding="utf-8"):
            if l.strip():
                metas.append(json.loads(l))
    seen = {}
    for m in metas:
        seen[(m["tag"], m["src"], m["strategy"], m.get("arm", "na"))] = m  # 取最新
    with open(occ_path, "w", encoding="utf-8") as f:
        f.write("src,lambda_per_min,profile,strategy,arm,commits,enqueued,commit_rate\n")
        for (tag, src, st, arm), m in sorted(seen.items()):
            if m.get("commits") is None or not m.get("enqueued"):
                continue
            lk = src.split("_")[1] if src.startswith("ovl_") else ""
            lam = LAMBDA.get(lk, "")
            profile = "drift" if src.endswith("drift") else (
                "uniform" if src.endswith("uniform") else "real/synthetic")
            f.write(f"{src},{lam},{profile},{st},{arm},{m['commits']},{m['enqueued']},"
                    f"{m['commits']/m['enqueued']:.4f}\n")
    print(f"→ {occ_path}")


if __name__ == "__main__":
    main()
