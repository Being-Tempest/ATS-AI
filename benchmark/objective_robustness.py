import csv
import json
import os
import statistics

from benchmark.merge_run import BASE_STREAMS, SEED_BASES, SEED_STREAMS
from benchmark.robustness_common import (
    DATA_DIR, RESULTS_DIR, STRATEGIES, load_frozen_scorebook,
    read_jsonl, response_states, run_metrics, sha256_file,
    validate_decision_scores,
)

WEIGHTS = [0, 0.25, 0.5, 0.75, 1]


def all_streams():
    return BASE_STREAMS + [f"{s}_s{seed}" for s in SEED_STREAMS for seed in SEED_BASES]


def stream_info(stream):
    base = stream.split("_s")[0]
    load = int(base.split("_")[1][1:])
    seed = int(stream.split("_s")[1]) if "_s" in stream else 42
    profile = "drift" if base.endswith("drift") else "uniform"
    primary = load in (12, 24) and profile == "drift"
    return load, seed, profile, primary


def utility_values(events, precise, batch, scorebook):
    values = {w: 0.0 for w in WEIGHTS}
    danmaku = [ev for ev in events if ev["type"] == "danmaku"]
    for ev in danmaku:
        score = scorebook[ev["text"]]
        if ev["id"] in precise:
            for w in WEIGHTS:
                values[w] += score
        elif ev["id"] in batch:
            for w in WEIGHTS:
                values[w] += w * score
    n = len(danmaku)
    return {w: values[w] / (80 * n) if n else None for w in WEIGHTS}


def longest_small_precise_streak(decisions, small_ids):
    longest = current = 0
    ordered = sorted(enumerate(decisions), key=lambda pair: (pair[1]["t"], pair[0]))
    for _, decision in ordered:
        is_small_precise = decision.get("type") != "batch" and decision.get("ev_id") in small_ids
        if is_small_precise:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def analyze_run(stream, strategy, scorebook):
    events = read_jsonl(os.path.join(DATA_DIR, f"{stream}.jsonl"))
    decisions = read_jsonl(os.path.join(
        RESULTS_DIR, f"decisions_merge_{stream}_{strategy}.jsonl"))
    precise, batch = response_states(decisions, events)
    validate_decision_scores(decisions, events, scorebook)
    utilities = utility_values(events, precise, batch, scorebook)
    gift_events = [ev for ev in events if ev["type"] in ("gift", "sc")]
    small = {ev["id"] for ev in gift_events if ev["price"] <= 100}
    high = {ev["id"] for ev in gift_events if ev["price"] > 100}
    responded = precise.keys() | batch.keys()
    load, seed, profile, primary = stream_info(stream)
    metrics = run_metrics(decisions, events, 7.8, scorebook)
    row = {
        "analysis_role": "primary" if primary else "descriptive",
        "stream": stream, "lambda_per_min": load, "profile": profile,
        "seed": seed, "strategy": strategy, "n_danmaku": metrics["n_danmaku"],
        "precise_n": metrics["precise_n"], "batch_covered_n": metrics["batch_covered_n"],
        "ignored_n": metrics["ignored_n"], "total_cov": metrics["total_cov"],
        "mean_precise_score": metrics["mean_precise_score"],
        "high_precise_rate": metrics["high_precise_rate"], "high_wait_s": metrics["high_wait"],
        "small_gift_n": len(small), "small_gift_precise_n": len(small & precise.keys()),
        "small_gift_responded_n": len(small & responded),
        "small_gift_precise_rate": len(small & precise.keys()) / len(small) if small else None,
        "small_gift_response_rate": len(small & responded) / len(small) if small else None,
        "longest_small_precise_streak": longest_small_precise_streak(decisions, small),
        "high_gift_n": len(high), "high_gift_precise_n": len(high & precise.keys()),
        "high_gift_responded_n": len(high & responded),
    }
    row.update({f"utility_w{str(w).replace('.', 'p')}": value for w, value in utilities.items()})
    return row


def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def utility_key(weight):
    return f"utility_w{str(weight).replace('.', 'p')}"


def fmt(value, digits=3):
    return "—" if value is None else f"{value:.{digits}f}"


def build_report(rows):
    primary = [r for r in rows if r["analysis_role"] == "primary"]
    lines = [
        "# R3 替代目标与交互行为模式代理",
        "",
        "使用原 k=2 基线决策，不与 R1 服务时间变化混杂。主分析仅 λ=12/24 drift 五种子；其余流只留在 CSV 作描述。",
        "",
        "`U(wB)=[Σprecise score+wB·Σbatch score]/(80N)`；N 为全部弹幕到达，未回复贡献 0，wP=1，wB=0/0.25/0.5/0.75/1。",
        "U 是覆盖加权判定分，不是观众满意度或真人感。小礼物 `price<=100` 电池，高价值礼物 `>100`。",
        "最长连续精回在每个运行内按全部回应动作排序，批量或任何其他精回都会中断；不跨种子拼接。",
        "较少逐条谢小礼物不自动等于更好，这些指标只称 interaction-pattern proxies。",
        "",
        "## 主分析：替代目标",
        "",
        "| λ | 策略 | U(0) | U(.25) | U(.5) | U(.75) | U(1) | 精回均分 | 总覆盖% |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    groups = {}
    for row in primary:
        groups.setdefault((row["lambda_per_min"], row["strategy"]), []).append(row)
    for key in sorted(groups):
        load, strategy = key
        group = groups[key]
        utilities = [fmt(statistics.mean(r[utility_key(w)] for r in group)) for w in WEIGHTS]
        scores = [r["mean_precise_score"] for r in group if r["mean_precise_score"] is not None]
        lines.append(
            f"| {load} | {strategy} | {' | '.join(utilities)} | "
            f"{fmt(statistics.mean(scores) if scores else None, 2)} | "
            f"{statistics.mean(r['total_cov']*100 for r in group):.2f} |"
        )
    lines += [
        "",
        "## 主分析：同种子相对 greedy 的效用差",
        "",
        "| λ | 策略 | ΔU(0) | ΔU(.25) | ΔU(.5) | ΔU(.75) | ΔU(1) |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for load in (12, 24):
        for strategy in STRATEGIES:
            if strategy == "greedy":
                continue
            group = groups[(load, strategy)]
            deltas = []
            for w in WEIGHTS:
                values = []
                for row in group:
                    greedy = next(r for r in primary if r["stream"] == row["stream"]
                                  and r["strategy"] == "greedy")
                    values.append(row[utility_key(w)] - greedy[utility_key(w)])
                deltas.append(f"{statistics.mean(values):+.3f} [{min(values):+.3f},{max(values):+.3f}]")
            lines.append(f"| {load} | {strategy} | {' | '.join(deltas)} |")
    lines += [
        "",
        "Δ 单元格为五种子配对差均值 [最小值,最大值]。",
        "",
        "## 主分析：交互行为模式代理",
        "",
        "| λ | 策略 | 小礼物到达 | 小礼物精回 | 小礼物总回应 | 最长连续小礼物精回（均值/最大） | 高价值到达/精回/总回应 |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    for key in sorted(groups):
        load, strategy = key
        group = groups[key]
        lines.append(
            f"| {load} | {strategy} | {sum(r['small_gift_n'] for r in group)} | "
            f"{sum(r['small_gift_precise_n'] for r in group)} | "
            f"{sum(r['small_gift_responded_n'] for r in group)} | "
            f"{statistics.mean(r['longest_small_precise_streak'] for r in group):.2f}/"
            f"{max(r['longest_small_precise_streak'] for r in group)} | "
            f"{sum(r['high_gift_n'] for r in group)}/{sum(r['high_gift_precise_n'] for r in group)}/"
            f"{sum(r['high_gift_responded_n'] for r in group)} |"
        )
    lines += [
        "",
        "## 逐运行明细（含描述性流）",
        "",
        "| 角色 | 流 | 策略 | N | U(0) | U(.5) | U(1) | 小礼物 到/精/总 | 最长连谢 | 高价值 到/精/总 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        lines.append(
            f"| {r['analysis_role']} | {r['stream']} | {r['strategy']} | {r['n_danmaku']} | "
            f"{fmt(r[utility_key(0)])} | {fmt(r[utility_key(0.5)])} | {fmt(r[utility_key(1)])} | "
            f"{r['small_gift_n']}/{r['small_gift_precise_n']}/{r['small_gift_responded_n']} | "
            f"{r['longest_small_precise_streak']} | "
            f"{r['high_gift_n']}/{r['high_gift_precise_n']}/{r['high_gift_responded_n']} |"
        )
    return "\n".join(lines) + "\n"


def write_manifest(rows):
    root = os.path.dirname(os.path.dirname(__file__))
    inputs = ["benchmark/robustness_common.py", "benchmark/objective_robustness.py",
              "benchmark/data/ovl_text_pool.jsonl", "benchmark/cache/score_danmaku.jsonl"]
    for stream in all_streams():
        inputs.append(f"benchmark/data/{stream}.jsonl")
        for strategy in STRATEGIES:
            inputs.append(f"benchmark/results/decisions_merge_{stream}_{strategy}.jsonl")
    outputs = ["objective_robustness.csv", "objective_robustness.md"]
    manifest = {
        "experiment": "R3_objective_robustness", "weights": WEIGHTS,
        "small_gift_price_max": 100, "rows": len(rows),
        "input_sha256": {p: sha256_file(os.path.join(root, p)) for p in inputs},
        "output_sha256": {p: sha256_file(os.path.join(RESULTS_DIR, p)) for p in outputs},
    }
    with open(os.path.join(RESULTS_DIR, "objective_robustness_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def main():
    scorebook = load_frozen_scorebook()
    rows = []
    for stream in all_streams():
        for strategy in STRATEGIES:
            row = analyze_run(stream, strategy, scorebook)
            rows.append(row)
        print(f"R3 {stream}: complete", flush=True)
    write_csv(os.path.join(RESULTS_DIR, "objective_robustness.csv"), rows)
    with open(os.path.join(RESULTS_DIR, "objective_robustness.md"), "w", encoding="utf-8") as f:
        f.write(build_report(rows))
    write_manifest(rows)
    print(f"R3 complete: {len(rows)} runs", flush=True)


if __name__ == "__main__":
    main()
