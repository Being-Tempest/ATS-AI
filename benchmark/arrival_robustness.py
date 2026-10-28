import argparse
import asyncio
import csv
import json
import math
import os
import random
import statistics

from benchmark.calibration_robustness import batch_k, make_duration_fn, precise_duration
from benchmark.overload_gen import MIX_DRIFT, POOL_PATH
from benchmark.replay import load_events_file
from benchmark.robustness_common import (
    DATA_DIR, RESULTS_DIR, SEEDS, STRATEGIES, fano_and_peak,
    load_frozen_scorebook, read_jsonl, run_metrics, run_strategy,
    scores_for_events, sha256_file, write_jsonl,
)
from benchmark.synth_gen import GIFT_TABLE, REGULARS, USERS

DURATION_S = 1800.0
LOADS = [12, 24]
PROCESSES = ["poisson", "onoff"]


def arrival_times(process, load, seed):
    rng = random.Random(seed * 100000 + load * 100 + 11)
    rate = load / 60.0
    times = []
    if process == "poisson":
        t = 0.0
        while True:
            t += rng.expovariate(rate)
            if t > DURATION_S:
                return times
            times.append(t)
    if process != "onoff":
        raise ValueError(process)
    t = 0.0
    on = rng.random() < 0.5
    while t < DURATION_S:
        state_end = min(DURATION_S, t + rng.expovariate(1 / 30.0))
        if on:
            while True:
                t += rng.expovariate(2 * rate)
                if t > state_end:
                    break
                times.append(t)
        t = state_end
        on = not on
    return times


def generate_stream(process, load, seed):
    times = arrival_times(process, load, seed)
    rng = random.Random(seed * 100000 + load * 100 + 29)
    pool = read_jsonl(POOL_PATH)
    buckets = {"high": [], "mid": [], "low": []}
    for row in pool:
        buckets[row["bucket"]].append(row["text"])
    gift_pool = []
    for gift, price, weight in GIFT_TABLE:
        gift_pool += [(gift, price)] * weight
    stream = f"onoff_{process}_l{load}_s{seed}"
    events = []
    for i, t in enumerate(times):
        seg = min(2, int(3 * t / DURATION_S))
        mix = MIX_DRIFT[seg]
        bucket = rng.choices(list(mix), weights=list(mix.values()))[0]
        text = rng.choice(buckets[bucket])
        user = rng.choice(REGULARS) if rng.random() < 0.25 else rng.choice(USERS)
        events.append({
            "id": f"{stream}-{i:05d}", "ts": round(t, 2), "type": "danmaku",
            "username": user, "text": text, "gift_name": "", "num": 1,
            "price": None, "recorded_score": None, "session": stream, "tag": bucket,
        })
        if rng.random() < 0.015:
            gift, price = rng.choice(gift_pool)
            gift_ts = round(t + 0.3, 2)
            if gift_ts <= DURATION_S:
                events.append({
                    "id": f"{stream}-g{i:05d}", "ts": gift_ts, "type": "gift",
                    "username": rng.choice(USERS + REGULARS), "text": "",
                    "gift_name": gift, "num": 1, "price": price,
                    "recorded_score": None, "session": stream,
                })
    events.sort(key=lambda ev: ev["ts"])
    return events


def flatten_row(process, load, seed, stream, strategy, events, metrics):
    fano10 = fano_and_peak(events, 10)
    fano1 = fano_and_peak(events, 1)
    peak60 = fano_and_peak(events, 60)
    return {
        "process": process, "lambda_target_per_min": load, "seed": seed,
        "stream": stream, "strategy": strategy,
        "n_danmaku": metrics["n_danmaku"], "n_gifts": metrics["n_gifts"],
        "actual_lambda_per_min": metrics["n_danmaku"] / 30.0,
        "fano_10s": fano10["fano"], "fano_1s": fano1["fano"],
        "peak_60s_per_min": peak60["peak_per_min"],
        "precise_n": metrics["precise_n"], "batch_covered_n": metrics["batch_covered_n"],
        "ignored_n": metrics["ignored_n"], "total_cov": metrics["total_cov"],
        "precise_rate": metrics["precise_rate"], "batch_cov_rate": metrics["batch_cov_rate"],
        "ignore_rate": metrics["ignore_rate"], "low_cov": metrics["tiers"]["low"]["cov"],
        "mid_cov": metrics["tiers"]["mid"]["cov"], "high_cov": metrics["tiers"]["high"]["cov"],
        "high_precise_rate": metrics["high_precise_rate"],
        "high_wait_s": metrics["high_wait"], "mean_wait_s": metrics["mean_wait"],
        "mean_precise_score": metrics["mean_precise_score"], "gift_cov": metrics["gift_cov"],
        "generation_calls": metrics["generation_calls"], "batch_n": metrics["batch_n"],
        "occupied_s": metrics["occupied_s"], "gap_s": metrics["gap_s"],
        "per_precise_slot_cov": metrics["per_precise_slot_cov"],
    }


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def fmt(value):
    return "—" if value is None else f"{value:.2f}"


def build_report(rows, smoke):
    lines = [
        "# R2 on/off 突发到达稳健性",
        "",
        "离线受控回放：1800 s，Poisson 对比两状态 MMPP；on/off 均值均 30 s，on 期到达率 2λ、off 期为 0。",
        "两臂是相同期望负载，不是相同实际 N；每个运行以自身全部弹幕为覆盖分母。drift 在 600/1200 s 切换。",
        "Fano 仅统计弹幕，使用全部完整非重叠窗口（含零窗）和样本方差 ddof=1；礼物另计。",
        "结果为补充稳健性证据，不外推到 Hawkes、自激励或真实高流量直播。",
        "",
    ]
    if smoke:
        lines += ["> **SMOKE：仅 λ=12、seed=42；不是完整实验结果。**", ""]
    groups = {}
    for row in rows:
        groups.setdefault((row["process"], row["lambda_target_per_min"], row["strategy"]), []).append(row)
    lines += [
        "## 各种子配对汇总",
        "",
        "| 过程 | λ | 策略 | 实际N | 实际λ | Fano10s | 60s峰值/min | 覆盖% | Δvs greedy(pp) | 高分精回% | 高分等待s | 精回均分 |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in sorted(groups):
        process, load, strategy = key
        group = groups[key]
        deltas = []
        for row in group:
            greedy = next(r for r in rows if r["process"] == process and r["stream"] == row["stream"]
                          and r["strategy"] == "greedy")
            deltas.append((row["total_cov"] - greedy["total_cov"]) * 100)
        waits = [r["high_wait_s"] for r in group if r["high_wait_s"] is not None]
        scores = [r["mean_precise_score"] for r in group if r["mean_precise_score"] is not None]
        lines.append(
            f"| {process} | {load} | {strategy} | {statistics.mean(r['n_danmaku'] for r in group):.1f} | "
            f"{statistics.mean(r['actual_lambda_per_min'] for r in group):.2f} | "
            f"{statistics.mean(r['fano_10s'] for r in group):.2f} | "
            f"{statistics.mean(r['peak_60s_per_min'] for r in group):.2f} | "
            f"{statistics.mean(r['total_cov']*100 for r in group):.2f} | {statistics.mean(deltas):+.2f} | "
            f"{statistics.mean(r['high_precise_rate']*100 for r in group):.2f} | "
            f"{fmt(statistics.mean(waits) if waits else None)} | "
            f"{fmt(statistics.mean(scores) if scores else None)} |"
        )
    lines += [
        "",
        "## 逐运行明细",
        "",
        "| 过程 | λ | 种子 | 策略 | N | Fano10s | 峰值/min | 精回 | 批覆盖 | 未回 | 覆盖% | 高分精回% | 高分等待s |",
        "|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['process']} | {r['lambda_target_per_min']} | {r['seed']} | {r['strategy']} | "
            f"{r['n_danmaku']} | {r['fano_10s']:.2f} | {r['peak_60s_per_min']:.1f} | "
            f"{r['precise_n']} | {r['batch_covered_n']} | {r['ignored_n']} | "
            f"{r['total_cov']*100:.2f} | {r['high_precise_rate']*100:.2f} | {fmt(r['high_wait_s'])} |"
        )
    return "\n".join(lines) + "\n"


def write_manifest(output_dir, rows, smoke):
    root = os.path.dirname(os.path.dirname(__file__))
    inputs = ["scheduler.py", "benchmark/replay.py", "benchmark/robustness_common.py",
              "benchmark/calibration_robustness.py", "benchmark/arrival_robustness.py",
              "benchmark/data/ovl_text_pool.jsonl", "benchmark/cache/score_danmaku.jsonl"]
    files = sorted(p for p in os.listdir(output_dir)
                   if p.startswith(("decisions_arrival_", "arrival_robustness"))
                   and p != "arrival_robustness_manifest.json")
    manifest = {
        "experiment": "R2_arrival_robustness", "smoke": smoke,
        "matrix": {"loads": LOADS, "seeds": [42] if smoke else SEEDS,
                   "strategies": STRATEGIES, "processes": PROCESSES},
        "input_sha256": {p: sha256_file(os.path.join(root, p)) for p in inputs},
        "output_sha256": {p: sha256_file(os.path.join(output_dir, p)) for p in files},
        "rows": len(rows),
    }
    with open(os.path.join(output_dir, "arrival_robustness_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    output_dir = os.path.join(RESULTS_DIR, "smoke_arrival") if args.smoke else RESULTS_DIR
    os.makedirs(output_dir, exist_ok=True)
    scorebook = load_frozen_scorebook()
    rows = []
    seeds = [42] if args.smoke else SEEDS
    for process in PROCESSES:
        for load in LOADS if not args.smoke else [12]:
            for seed in seeds:
                raw_events = generate_stream(process, load, seed)
                stream = f"onoff_{process}_l{load}_s{seed}"
                data_path = os.path.join(DATA_DIR, f"{stream}.jsonl")
                write_jsonl(data_path, raw_events)
                events = load_events_file(data_path)
                scores = scores_for_events(events, scorebook)
                for strategy in STRATEGIES:
                    decisions, _ = await run_strategy(
                        events, scores, strategy, "arrival", make_duration_fn("H0"))
                    metrics = run_metrics(decisions, raw_events, precise_duration("H0"), scorebook)
                    out = os.path.join(output_dir, f"decisions_arrival_{process}_l{load}_s{seed}_{strategy}.jsonl")
                    write_jsonl(out, decisions)
                    rows.append(flatten_row(process, load, seed, stream, strategy, raw_events, metrics))
                    print(f"R2 {process} {stream} {strategy}: N={metrics['n_danmaku']} "
                          f"coverage={metrics['total_cov']:.4f}", flush=True)
    for row in rows:
        greedy = next(r for r in rows if r["process"] == row["process"]
                      and r["stream"] == row["stream"] and r["strategy"] == "greedy")
        row["coverage_delta_vs_greedy"] = row["total_cov"] - greedy["total_cov"]
        if row["process"] == "onoff":
            poisson_stream = row["stream"].replace("onoff_onoff_", "onoff_poisson_")
            paired = next((r for r in rows if r["stream"] == poisson_stream
                           and r["strategy"] == row["strategy"]), None)
            row["onoff_minus_poisson_cov"] = (row["total_cov"] - paired["total_cov"]) if paired else None
        else:
            row["onoff_minus_poisson_cov"] = None
    write_csv(os.path.join(output_dir, "arrival_robustness.csv"), rows)
    with open(os.path.join(output_dir, "arrival_robustness.md"), "w", encoding="utf-8") as f:
        f.write(build_report(rows, args.smoke))
    write_manifest(output_dir, rows, args.smoke)
    print(f"R2 complete: {len(rows)} runs -> {output_dir}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
