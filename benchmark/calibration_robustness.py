import argparse
import asyncio
import csv
import json
import math
import os
import statistics

from benchmark.replay import load_events_file
from benchmark.robustness_common import (
    DATA_DIR, RESULTS_DIR, SEEDS, STRATEGIES, load_frozen_scorebook,
    mean_sd, read_jsonl, run_metrics, run_strategy, scores_for_events,
    sha256_file, write_jsonl,
)

SCENARIOS = {
    "H0": {"precise_s": 7.8, "speech_chars_s": None, "overhead_s": 0.0},
    "P1": {"precise_s": 9.0, "speech_chars_s": 4.72, "overhead_s": 3.8},
    "P2": {"precise_s": 9.0, "speech_chars_s": 3.83, "overhead_s": 3.8},
}
LOADS = [12, 24]


def batch_duration(scenario, n):
    cfg = SCENARIOS[scenario]
    if scenario == "H0":
        return 14.8
    return (49.8 + 2.12 * n) / cfg["speech_chars_s"] + cfg["overhead_s"]


def precise_duration(scenario):
    return SCENARIOS[scenario]["precise_s"]


def batch_k(scenario, n):
    return math.ceil(batch_duration(scenario, n) / precise_duration(scenario))


def make_duration_fn(scenario):
    def duration(item, reply):
        item_type = getattr(item, "item_type", getattr(item, "type", None))
        if item_type == "batch":
            return batch_duration(scenario, len(item.covered or []))
        return precise_duration(scenario)
    return duration


def stream_name(load, seed):
    base = f"ovl_l{load}_drift"
    return base if seed == 42 else f"{base}_s{seed}"


def decision_path(output_dir, scenario, stream, strategy):
    return os.path.join(output_dir, f"decisions_calibration_{scenario}_{stream}_{strategy}.jsonl")


def annotate_batches(decisions, scenario):
    for decision in decisions:
        if decision["type"] != "batch":
            continue
        n = len(decision.get("covered", []))
        exact_duration = batch_duration(scenario, n)
        assert abs(decision["duration"] - round(exact_duration, 2)) < 1e-9
        decision["batch_n"] = n
        decision["batch_duration_model_s"] = exact_duration
        decision["precise_duration_s"] = precise_duration(scenario)
        decision["k_batch"] = batch_k(scenario, n)


def slot_fn(scenario):
    def slots(decision):
        if decision["type"] == "batch":
            return batch_k(scenario, len(decision.get("covered", [])))
        assert abs(decision["duration"] - precise_duration(scenario)) < 1e-9
        return 1
    return slots


def flatten_row(scenario, load, seed, stream, strategy, metrics):
    high_wait = metrics["high_wait"]
    mean_wait = metrics["mean_wait"]
    precise_score = metrics["mean_precise_score"]
    return {
        "scenario": scenario, "lambda_per_min": load, "seed": seed,
        "stream": stream, "strategy": strategy,
        "n_danmaku": metrics["n_danmaku"], "n_gifts": metrics["n_gifts"],
        "precise_n": metrics["precise_n"], "batch_covered_n": metrics["batch_covered_n"],
        "ignored_n": metrics["ignored_n"],
        "total_cov": metrics["total_cov"], "precise_rate": metrics["precise_rate"],
        "batch_cov_rate": metrics["batch_cov_rate"], "ignore_rate": metrics["ignore_rate"],
        "low_cov": metrics["tiers"]["low"]["cov"],
        "mid_cov": metrics["tiers"]["mid"]["cov"],
        "high_cov": metrics["tiers"]["high"]["cov"],
        "high_precise_rate": metrics["high_precise_rate"],
        "high_wait_s": high_wait, "mean_wait_s": mean_wait,
        "mean_precise_score": precise_score,
        "gift_cov": metrics["gift_cov"], "gift_responded_n": metrics["gift_responded_n"],
        "generation_calls": metrics["generation_calls"], "batch_n": metrics["batch_n"],
        "batch_avg_size": metrics["batch_avg"], "occupied_s": metrics["occupied_s"],
        "span_s": metrics["span_s"], "gap_s": metrics["gap_s"],
        "equivalent_slots": metrics["equivalent_slots"],
        "per_precise_slot_cov": metrics["per_precise_slot_cov"],
        "batch_k_min": metrics["batch_k_min"], "batch_k_max": metrics["batch_k_max"],
        "batch_k_mean": metrics["batch_k_mean"],
    }


def add_batch_k_metrics(decisions, metrics):
    ks = [d["k_batch"] for d in decisions if d["type"] == "batch"]
    metrics["batch_k_min"] = min(ks) if ks else None
    metrics["batch_k_max"] = max(ks) if ks else None
    metrics["batch_k_mean"] = statistics.mean(ks) if ks else None


def write_csv(path, rows):
    fields = list(rows[0])
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def fmt(value, digits=2):
    if value is None:
        return "—"
    return f"{value:.{digits}f}"


def build_report(rows, smoke):
    lines = [
        "# R1 批处理服务时间与 k_batch 稳健性",
        "",
        "本报告为离线补充稳健性分析，不是原实验假设的替换。所有策略共享冻结文本与评分；无真实 API、开播或 VTS。",
        "",
        "- H0：精回总占用 7.8 s，批量总占用 14.8 s。",
        "- P1：精回总占用 9.0 s；批量 `(49.8+2.12n)/4.72+3.8` s。",
        "- P2：精回总占用 9.0 s；批量 `(49.8+2.12n)/3.83+3.8` s。",
        "- 9.0 s 已包含精回固定开销；3.8 s 仅为批量非播放开销的情景假设。",
        "- 曲线测量支持域仅为 q2d、n=2..10；对其他批型及域外 n 的统一公式属于情景外推，不是专属实测。",
        "- H0→P1/P2 同时改变精回与批量模型，因此是联合情景对比。逐批 k 用未取整时长计算。",
        "- 实际占用=`Σdecision.duration`；gap 为首到达至最后播放结束的时间跨度减实际占用。",
        "",
    ]
    if smoke:
        lines += ["> **SMOKE：仅 λ=12、seed=42；不是完整实验结果。**", ""]
    groups = {}
    for row in rows:
        groups.setdefault((row["scenario"], row["lambda_per_min"], row["strategy"]), []).append(row)
    lines += [
        "## 各种子配对汇总",
        "",
        "覆盖为百分比；Δ 为同流同种子相对 greedy 的配对差（pp）。均值±SD 后的方括号是最小/最大种子值，不作独立文本样本推断。",
        "",
        "| 情景 | λ | 策略 | 总覆盖均值±SD | Δ覆盖 vs greedy | 低分覆盖 | 高分精回 | 高分等待(s) | k范围 | 占用(s) |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for key in sorted(groups):
        scenario, load, strategy = key
        group = groups[key]
        cov = [r["total_cov"] * 100 for r in group]
        low = [r["low_cov"] * 100 for r in group]
        hp = [r["high_precise_rate"] * 100 for r in group]
        waits = [r["high_wait_s"] for r in group if r["high_wait_s"] is not None]
        deltas = []
        for row in group:
            greedy = next(r for r in rows if r["scenario"] == scenario
                          and r["stream"] == row["stream"] and r["strategy"] == "greedy")
            deltas.append((row["total_cov"] - greedy["total_cov"]) * 100)
        k_values = [r["batch_k_max"] for r in group if r["batch_k_max"] is not None]
        k_range = f"{min(r['batch_k_min'] for r in group if r['batch_k_min'] is not None)}–{max(k_values)}" if k_values else "—"
        lines.append(
            f"| {scenario} | {load} | {strategy} | {statistics.mean(cov):.2f}±{statistics.stdev(cov) if len(cov)>1 else 0:.2f} "
            f"[{min(cov):.2f},{max(cov):.2f}] | {statistics.mean(deltas):+.2f} | "
            f"{statistics.mean(low):.2f} | {statistics.mean(hp):.2f} | "
            f"{fmt(statistics.mean(waits) if waits else None)} | {k_range} | "
            f"{statistics.mean(r['occupied_s'] for r in group):.1f} |"
        )
    lines += [
        "",
        "## 逐运行明细",
        "",
        "| 情景 | λ | 种子 | 策略 | N | 精回 | 批覆盖 | 未回 | 覆盖% | 低分% | 高分精回% | 高分等待s | 批次 | k范围 | 占用s |",
        "|---|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        k_range = f"{r['batch_k_min']}–{r['batch_k_max']}" if r["batch_k_max"] is not None else "—"
        lines.append(
            f"| {r['scenario']} | {r['lambda_per_min']} | {r['seed']} | {r['strategy']} | "
            f"{r['n_danmaku']} | {r['precise_n']} | {r['batch_covered_n']} | {r['ignored_n']} | "
            f"{r['total_cov']*100:.2f} | {r['low_cov']*100:.2f} | {r['high_precise_rate']*100:.2f} | "
            f"{fmt(r['high_wait_s'])} | {r['batch_n']} | {k_range} | {r['occupied_s']:.1f} |"
        )
    return "\n".join(lines) + "\n"


def write_manifest(output_dir, rows, smoke):
    inputs = ["scheduler.py", "experiments/replay.py", "experiments/robustness_common.py",
              "experiments/calibration_robustness.py", "experiments/data/ovl_text_pool.jsonl",
              "experiments/cache/score_danmaku.jsonl"]
    root = os.path.dirname(os.path.dirname(__file__))
    files = sorted(p for p in os.listdir(output_dir)
                   if p.startswith(("decisions_calibration_", "calibration_robustness"))
                   and p != "calibration_robustness_manifest.json")
    manifest = {
        "experiment": "R1_calibration_robustness", "smoke": smoke,
        "matrix": {"loads": LOADS, "seeds": [42] if smoke else SEEDS,
                   "strategies": STRATEGIES, "scenarios": list(SCENARIOS)},
        "input_sha256": {p: sha256_file(os.path.join(root, p)) for p in inputs},
        "output_sha256": {p: sha256_file(os.path.join(output_dir, p)) for p in files},
        "rows": len(rows),
    }
    with open(os.path.join(output_dir, "calibration_robustness_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    output_dir = os.path.join(RESULTS_DIR, "smoke_calibration") if args.smoke else RESULTS_DIR
    os.makedirs(output_dir, exist_ok=True)
    scorebook = load_frozen_scorebook()
    rows = []
    seeds = [42] if args.smoke else SEEDS
    for scenario in SCENARIOS:
        for load in LOADS if not args.smoke else [12]:
            for seed in seeds:
                stream = stream_name(load, seed)
                path = os.path.join(DATA_DIR, f"{stream}.jsonl")
                raw_events = read_jsonl(path)
                events = load_events_file(path)
                scores = scores_for_events(events, scorebook)
                for strategy in STRATEGIES:
                    decisions, _ = await run_strategy(
                        events, scores, strategy, "calibration", make_duration_fn(scenario))
                    annotate_batches(decisions, scenario)
                    metrics = run_metrics(decisions, raw_events, precise_duration(scenario),
                                          scorebook, slot_fn(scenario))
                    add_batch_k_metrics(decisions, metrics)
                    write_jsonl(decision_path(output_dir, scenario, stream, strategy), decisions)
                    rows.append(flatten_row(scenario, load, seed, stream, strategy, metrics))
                    print(f"R1 {scenario} {stream} {strategy}: coverage={metrics['total_cov']:.4f}", flush=True)
    for row in rows:
        greedy = next(r for r in rows if r["scenario"] == row["scenario"]
                      and r["stream"] == row["stream"] and r["strategy"] == "greedy")
        row["coverage_delta_vs_greedy"] = row["total_cov"] - greedy["total_cov"]
    write_csv(os.path.join(output_dir, "calibration_robustness.csv"), rows)
    with open(os.path.join(output_dir, "calibration_robustness.md"), "w", encoding="utf-8") as f:
        f.write(build_report(rows, args.smoke))
    write_manifest(output_dir, rows, args.smoke)
    print(f"R1 complete: {len(rows)} runs -> {output_dir}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
