"""
过载+漂移实验主跑器（预注册 benchmark/preregistration.md）

6 策略 × 8 流 × 占坑双臂：
  - 基线（fifo/random/keyword/greedy）无占坑概念，每流跑一次
  - 调度器策略（fixed50/full）× 两臂（commit=线上真实配置 / nocommit=禁用占坑）
另：E4 判别性阈值扫描在 λ≥2μ+drift 流上（commit 臂，20-80 步长 5 + 自适应）

用法：
  python -m benchmark.overload_run             # 主矩阵
  python -m benchmark.overload_run --scan      # 只做 E4 扫描
输出命名：decisions_ovl_<src>_<strategy>.jsonl（基线）
         decisions_ovl_<src>_<strategy>_<arm>.jsonl（调度器策略，arm=commit|nocommit）
"""

import argparse
import asyncio
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmark.replay import (load_events_file, prescore_events, replay_scheduler,
                                replay_baseline, write_jsonl, RESULTS_DIR)
from benchmark.api_cache import ApiCache

STREAMS = [f"ovl_{l}_{p}" for l in ("l3", "l6", "l12", "l24")
           for p in ("uniform", "drift")]
BASELINES = ["fifo", "random", "keyword", "greedy"]
SCAN_STREAMS = ["ovl_l12_drift", "ovl_l24_drift"]
SCAN_THRESHOLDS = [20 + 5 * i for i in range(13)]  # 20..80


def run_meta(tag, src, strategy, arm, commits, enqueued, n_dec):
    with open(os.path.join(RESULTS_DIR, "run_meta.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "tag": tag, "src": src, "strategy": strategy, "arm": arm,
            "commits": commits, "enqueued": enqueued, "decisions": n_dec,
            "scorer_model": "deepseek-v4-flash", "reply_model": None,
            "reply_duration_s": 9.0,
            "run_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False) + "\n")


async def run_stream(src, cache, strategies=None, arms=("commit", "nocommit")):
    events = load_events_file(os.path.join("experiments", "data", f"{src}.jsonl"))
    scores = await prescore_events(events, cache)
    n_gifts = sum(1 for e in events if e.type in ("gift", "sc"))
    for st in (strategies or BASELINES):
        if st == "full_merge":
            continue   # 走下方批量合并分支
        decisions = await replay_baseline(events, scores, st)
        out = os.path.join(RESULTS_DIR, f"decisions_ovl_{src}_{st}.jsonl")
        write_jsonl(out, decisions)
        run_meta("ovl", src, st, "na", None, None, len(decisions))
        print(f"[{src}/{st}] {len(decisions)} 条", flush=True)
    for arm in arms:
        for st, ft in (("fixed50", 50.0), ("full", None)):
            if strategies and st not in strategies:
                continue
            name = f"{st}_{arm}"
            decisions, trace, sched = await replay_scheduler(
                events, scores, "ovl", st, fixed_threshold=ft, variant=name,
                disable_commit=(arm == "nocommit"))
            out = os.path.join(RESULTS_DIR, f"decisions_ovl_{src}_{name}.jsonl")
            write_jsonl(out, decisions)
            run_meta("ovl", src, st, arm, sched.stats["commit"],
                     sched.stats["enqueue_danmaku"] + sched.stats["enqueue_gift"],
                     len(decisions))
            print(f"[{src}/{name}] {len(decisions)} 条，占坑 {sched.stats['commit']}",
                  flush=True)
            if st == "full":
                tp = os.path.join(RESULTS_DIR, f"threshold_trace_ovl_{src}_{arm}.jsonl")
                write_jsonl(tp, trace)
    # ── 批量合并变体（预注册 §8，H5-H7）：full + batch_merge=True，占坑开臂 ──
    if strategies and "full_merge" in strategies:
        decisions, trace, sched = await replay_scheduler(
            events, scores, "ovl", "full", variant="full_merge_commit",
            batch_merge=True)
        out = os.path.join(RESULTS_DIR, f"decisions_ovl_{src}_full_merge_commit.jsonl")
        write_jsonl(out, decisions)
        run_meta("ovl", src, "full_merge", "commit", sched.stats["commit"],
                 sched.stats["enqueue_danmaku"] + sched.stats["enqueue_gift"],
                 len(decisions))
        n_batch = sum(sched.stats[k] for k in ("batch_q2d", "batch_q2g", "batch_q3"))
        print(f"[{src}/full_merge_commit] {len(decisions)} 条，批量 {n_batch} 次 "
              f"(q2d={sched.stats['batch_q2d']} q2g={sched.stats['batch_q2g']} "
              f"q3={sched.stats['batch_q3']})", flush=True)


async def run_scan(cache):
    import csv
    for src in SCAN_STREAMS:
        events = load_events_file(os.path.join("experiments", "data", f"{src}.jsonl"))
        scores = await prescore_events(events, cache)
        total_gifts = sum(1 for e in events if e.type in ("gift", "sc"))
        out_path = os.path.join(RESULTS_DIR, f"threshold_scan_{src}.csv")
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["variant", "threshold", "replies", "replied_dm",
                        "avg_dm_score", "gift_total", "gift_replied", "promotes"])
            for th in SCAN_THRESHOLDS + [None]:
                name = "adaptive" if th is None else f"fixed{th}"
                decisions, trace, sched = await replay_scheduler(
                    events, scores, "ovlscan",
                    "full" if th is None else "fixed",
                    fixed_threshold=th, variant=name)
                dm = [d for d in decisions if d["type"] == "danmaku"]
                gf = [d for d in decisions if d["type"] in ("gift", "sc")]
                avg = sum(d["score"] for d in dm) / len(dm) if dm else 0.0
                promotes = sum(1 for r in trace if r["event"] == "promote")
                w.writerow([name, "" if th is None else th, len(decisions), len(dm),
                            round(avg, 2), total_gifts, len(gf), promotes])
                f.flush()
                print(f"[scan {src}/{name}] 回复 {len(decisions)} 均分 {avg:.1f} "
                      f"升舱 {promotes}", flush=True)
        print(f"→ {out_path}", flush=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true", help="只跑 E4 阈值扫描")
    ap.add_argument("--streams", nargs="*", default=STREAMS)
    args = ap.parse_args()

    from scorer import score_danmaku
    cache = ApiCache("score_danmaku", score_danmaku, enabled=True)

    if args.scan:
        await run_scan(cache)
        return
    for src in args.streams:
        print(f"=== {src} ===", flush=True)
        await run_stream(src, cache)
    print(cache.stats())


if __name__ == "__main__":
    asyncio.run(main())
