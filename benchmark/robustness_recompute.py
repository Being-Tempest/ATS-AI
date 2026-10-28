"""Independently recompute R1/R2/R3 robustness metrics using only the stdlib."""

import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import sys


EXPERIMENT_DIR = Path(__file__).resolve().parent
ROOT_DIR = EXPERIMENT_DIR.parent
DATA_DIR = EXPERIMENT_DIR / "data"
RESULTS_DIR = EXPERIMENT_DIR / "results"
STRATEGIES = ["fifo", "greedy", "full_commit", "full_merge_commit", "full_merge_q3_commit"]
SEEDS = [42, 1001, 2002, 3003, 4004]
WEIGHTS = [0, 0.25, 0.5, 0.75, 1]
ABS_TOL = 1e-9
REL_TOL = 1e-12

R1_FIELDS = [
    "scenario", "lambda_per_min", "seed", "stream", "strategy",
    "n_danmaku", "n_gifts", "precise_n", "batch_covered_n", "ignored_n",
    "total_cov", "precise_rate", "batch_cov_rate", "ignore_rate",
    "low_cov", "mid_cov", "high_cov", "high_precise_rate", "high_wait_s",
    "mean_wait_s", "mean_precise_score", "gift_cov", "gift_responded_n",
    "generation_calls", "batch_n", "batch_avg_size", "occupied_s", "span_s",
    "gap_s", "equivalent_slots", "per_precise_slot_cov", "batch_k_min",
    "batch_k_max", "batch_k_mean", "coverage_delta_vs_greedy",
]
R2_FIELDS = [
    "process", "lambda_target_per_min", "seed", "stream", "strategy",
    "n_danmaku", "n_gifts", "actual_lambda_per_min", "fano_10s", "fano_1s",
    "peak_60s_per_min", "precise_n", "batch_covered_n", "ignored_n",
    "total_cov", "precise_rate", "batch_cov_rate", "ignore_rate", "low_cov",
    "mid_cov", "high_cov", "high_precise_rate", "high_wait_s", "mean_wait_s",
    "mean_precise_score", "gift_cov", "generation_calls", "batch_n",
    "occupied_s", "gap_s", "per_precise_slot_cov", "coverage_delta_vs_greedy",
    "onoff_minus_poisson_cov",
]
R3_FIELDS = [
    "analysis_role", "stream", "lambda_per_min", "profile", "seed", "strategy",
    "n_danmaku", "precise_n", "batch_covered_n", "ignored_n", "total_cov",
    "mean_precise_score", "high_precise_rate", "high_wait_s", "small_gift_n",
    "small_gift_precise_n", "small_gift_responded_n", "small_gift_precise_rate",
    "small_gift_response_rate", "longest_small_precise_streak", "high_gift_n",
    "high_gift_precise_n", "high_gift_responded_n", "utility_w0",
    "utility_w0p25", "utility_w0p5", "utility_w0p75", "utility_w1",
]


def relative_path(path):
    try:
        return Path(path).resolve().relative_to(ROOT_DIR).as_posix()
    except ValueError:
        return Path(path).as_posix()


def json_value(value):
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def canonical_type(item_type):
    return "gift" if item_type in ("gift", "sc") else item_type


def mean(values):
    values = list(values)
    return math.fsum(values) / len(values) if values else None


def ratio(numerator, denominator, empty=0.0):
    return numerator / denominator if denominator else empty


def utility_key(weight):
    return f"utility_w{str(weight).replace('.', 'p')}"


def r1_batch_duration(scenario, n):
    if scenario == "H0":
        return 14.8
    speech_rate = {"P1": 4.72, "P2": 3.83}[scenario]
    return (49.8 + 2.12 * n) / speech_rate + 3.8


def r1_precise_duration(scenario):
    return 7.8 if scenario == "H0" else 9.0


def r1_stream(load, seed):
    base = f"ovl_l{load}_drift"
    return base if seed == 42 else f"{base}_s{seed}"


def r3_streams():
    base = [f"ovl_l{load}_{profile}" for load in (3, 6, 12, 24)
            for profile in ("uniform", "drift")]
    seeded = [f"ovl_l{load}_drift_s{seed}" for load in (12, 24)
              for seed in (1001, 2002, 3003, 4004)]
    return base + seeded


class Report:
    def __init__(self, smoke):
        self.mode = "smoke" if smoke else "full"
        self.differences = []
        self.input_sha256 = {}
        self.summaries = {}

    def add(self, experiment, run, kind, field, expected, actual):
        self.differences.append({
            "experiment": experiment,
            "run": run,
            "kind": kind,
            "field": field,
            "expected": json_value(expected),
            "actual": json_value(actual),
        })

    def missing(self, experiment, run, path):
        self.add(experiment, run, "missing_input", relative_path(path), "file exists", "missing")

    def read_bytes(self, path, experiment, run):
        try:
            payload = Path(path).read_bytes()
        except FileNotFoundError:
            self.missing(experiment, run, path)
            return None
        except OSError as exc:
            self.add(experiment, run, "read_error", relative_path(path), "readable file", str(exc))
            return None
        self.input_sha256[relative_path(path)] = hashlib.sha256(payload).hexdigest()
        return payload

    def read_jsonl(self, path, experiment, run):
        payload = self.read_bytes(path, experiment, run)
        if payload is None:
            return None
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            self.add(experiment, run, "parse_error", relative_path(path), "UTF-8 JSONL", str(exc))
            return None
        rows = []
        for line_number, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line, object_pairs_hook=unique_object)
            except (ValueError, TypeError) as exc:
                self.add(experiment, run, "parse_error",
                         f"{relative_path(path)}:{line_number}", "valid JSON object", str(exc))
                return None
            if not isinstance(row, dict):
                self.add(experiment, run, "parse_error",
                         f"{relative_path(path)}:{line_number}", "JSON object", type(row).__name__)
                return None
            rows.append(row)
        return rows

    def read_csv(self, path, experiment, expected_fields):
        payload = self.read_bytes(path, experiment, None)
        if payload is None:
            return None
        try:
            text = payload.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text, newline=""))
            headers = reader.fieldnames
            if headers is None:
                self.add(experiment, None, "csv_structure", "header", "non-empty CSV header", "missing")
                return None
            if len(headers) != len(set(headers)):
                duplicates = sorted({name for name in headers if headers.count(name) > 1})
                self.add(experiment, None, "csv_structure", "header",
                         "unique column names", duplicates)
                return None
            for field in expected_fields:
                if field not in headers:
                    self.add(experiment, None, "csv_structure", field, "column exists", "missing")
            for field in headers:
                if field not in expected_fields:
                    self.add(experiment, None, "csv_structure", field, "no unexpected column", field)
            return headers, list(reader)
        except (UnicodeDecodeError, csv.Error) as exc:
            self.add(experiment, None, "parse_error", relative_path(path), "valid UTF-8 CSV", str(exc))
            return None

    def finish_summary(self, experiment, expected, recomputed, checked):
        self.summaries[experiment] = {
            "expected_runs": expected,
            "recomputed_runs": recomputed,
            "checked_runs": checked,
            "mismatch_count": sum(d["experiment"] == experiment for d in self.differences),
        }


def compare_field(report, experiment, run, field, expected, actual):
    if expected is None:
        ok = actual is None or actual.strip() == ""
    elif isinstance(expected, int) and not isinstance(expected, bool):
        try:
            ok = actual is not None and int(actual) == expected
        except (TypeError, ValueError):
            ok = False
    elif isinstance(expected, float):
        try:
            value = float(actual)
            ok = math.isfinite(value) and math.isclose(
                value, expected, rel_tol=REL_TOL, abs_tol=ABS_TOL)
        except (TypeError, ValueError):
            ok = False
    else:
        ok = actual == expected
    if not ok:
        report.add(experiment, run, "field_mismatch", field, expected, actual)


def build_states(report, experiment, run_name, events, decisions, scorebook, spec):
    valid = True
    event_by_id = {}
    for index, event in enumerate(events):
        event_id = event.get("id")
        event_type = event.get("type")
        if not isinstance(event_id, str) or not event_id:
            report.add(experiment, run_name, "integrity", f"events[{index}].id",
                       "non-empty unique event ID", event_id)
            valid = False
            continue
        if event_id in event_by_id:
            report.add(experiment, run_name, "integrity", f"events[{index}].id",
                       "unique event ID; no de-duplication", event_id)
            valid = False
            continue
        if canonical_type(event_type) not in ("danmaku", "gift"):
            report.add(experiment, run_name, "integrity", f"events[{index}].type",
                       "danmaku/gift/sc", event_type)
            valid = False
        try:
            timestamp = float(event["ts"])
            if not math.isfinite(timestamp):
                raise ValueError("non-finite timestamp")
        except (KeyError, TypeError, ValueError) as exc:
            report.add(experiment, run_name, "integrity", f"events[{index}].ts",
                       "finite numeric timestamp", str(exc))
            valid = False
        event_by_id[event_id] = event
    if not valid or len(event_by_id) != len(events):
        return None

    states = {}
    precise_duration = r1_precise_duration(spec["scenario"]) if experiment == "R1" else None
    for decision_index, decision in enumerate(decisions):
        decision_type = decision.get("type")
        try:
            decision_t = float(decision["t"])
            duration = float(decision["duration"])
            if not math.isfinite(decision_t) or not math.isfinite(duration) or duration < 0:
                raise ValueError("non-finite time or negative duration")
        except (KeyError, TypeError, ValueError) as exc:
            report.add(experiment, run_name, "integrity",
                       f"decisions[{decision_index}] time/duration",
                       "finite t and finite non-negative duration", str(exc))
            valid = False
            continue
        if decision_type == "batch":
            items = decision.get("covered")
            if not isinstance(items, list):
                report.add(experiment, run_name, "integrity",
                           f"decisions[{decision_index}].covered", "list", items)
                valid = False
                continue
        elif canonical_type(decision_type) in ("danmaku", "gift"):
            items = [decision]
        else:
            report.add(experiment, run_name, "integrity",
                       f"decisions[{decision_index}].type", "batch/danmaku/gift/sc", decision_type)
            valid = False
            continue

        if experiment == "R1":
            if decision_type == "batch":
                n = len(items)
                model_duration = r1_batch_duration(spec["scenario"], n)
                expected_k = math.ceil(model_duration / precise_duration)
                annotations = {
                    "batch_n": n,
                    "batch_duration_model_s": model_duration,
                    "precise_duration_s": precise_duration,
                    "k_batch": expected_k,
                }
                for field, expected in annotations.items():
                    actual = decision.get(field)
                    if isinstance(expected, float):
                        try:
                            matches = math.isclose(float(actual), expected,
                                                   rel_tol=REL_TOL, abs_tol=ABS_TOL)
                        except (TypeError, ValueError):
                            matches = False
                    else:
                        matches = actual == expected
                    if not matches:
                        report.add(experiment, run_name, "integrity",
                                   f"decisions[{decision_index}].{field}", expected, actual)
                        valid = False
                if not math.isclose(duration, round(model_duration, 2),
                                    rel_tol=REL_TOL, abs_tol=ABS_TOL):
                    report.add(experiment, run_name, "integrity",
                               f"decisions[{decision_index}].duration",
                               round(model_duration, 2), decision.get("duration"))
                    valid = False
            elif not math.isclose(duration, precise_duration,
                                  rel_tol=REL_TOL, abs_tol=ABS_TOL):
                report.add(experiment, run_name, "integrity",
                           f"decisions[{decision_index}].duration",
                           precise_duration, decision.get("duration"))
                valid = False

        for item_index, item in enumerate(items):
            location = (f"decisions[{decision_index}].covered[{item_index}]"
                        if decision_type == "batch" else f"decisions[{decision_index}]")
            event_id = item.get("ev_id")
            if event_id not in event_by_id:
                report.add(experiment, run_name, "integrity", f"{location}.ev_id",
                           "known input event ID", event_id)
                valid = False
                continue
            event = event_by_id[event_id]
            expected_type = canonical_type(event["type"])
            actual_type = canonical_type(item.get("type"))
            if actual_type != expected_type:
                report.add(experiment, run_name, "integrity", f"{location}.type",
                           expected_type, item.get("type"))
                valid = False
            if event_id in states:
                report.add(experiment, run_name, "integrity", f"{location}.ev_id",
                           "one final response state; no de-duplication", event_id)
                valid = False
                continue
            if expected_type == "danmaku":
                text = event.get("text")
                if text not in scorebook:
                    report.add(experiment, run_name, "integrity", f"{location}.text",
                               "text present in frozen scorebook", text)
                    valid = False
                    continue
                try:
                    score_matches = int(item["score"]) == scorebook[text]
                except (KeyError, TypeError, ValueError):
                    score_matches = False
                if not score_matches:
                    report.add(experiment, run_name, "integrity", f"{location}.score",
                               scorebook[text], item.get("score"))
                    valid = False
            states[event_id] = {
                "state": "batch" if decision_type == "batch" else "precise",
                "t": decision_t,
                "decision_index": decision_index,
            }

    dm_ids = {event["id"] for event in events if event["type"] == "danmaku"}
    gift_ids = {event["id"] for event in events if canonical_type(event["type"]) == "gift"}
    for label, ids in (("danmaku", dm_ids), ("gift", gift_ids)):
        precise_ids = {event_id for event_id in ids
                       if states.get(event_id, {}).get("state") == "precise"}
        batch_ids = {event_id for event_id in ids
                     if states.get(event_id, {}).get("state") == "batch"}
        ignored_ids = ids - precise_ids - batch_ids
        if len(precise_ids) + len(batch_ids) + len(ignored_ids) != len(ids):
            report.add(experiment, run_name, "conservation", label,
                       "precise + batch + ignored == N",
                       f"{len(precise_ids)} + {len(batch_ids)} + {len(ignored_ids)} != {len(ids)}")
            valid = False
    return states if valid else None


def bucket_for(event, scorebook):
    tag = event.get("tag")
    if tag:
        if tag not in ("low", "mid", "high"):
            raise ValueError(f"unknown danmaku bucket: {tag}")
        return tag
    score = scorebook[event["text"]]
    return "high" if score >= 60 else ("mid" if score >= 30 else "low")


def common_metrics(events, decisions, states, scorebook):
    event_by_id = {event["id"]: event for event in events}
    dm_ids = {event["id"] for event in events if event["type"] == "danmaku"}
    gift_ids = {event["id"] for event in events if canonical_type(event["type"]) == "gift"}
    precise = {event_id for event_id, value in states.items() if value["state"] == "precise"}
    batch = {event_id for event_id, value in states.items() if value["state"] == "batch"}
    precise_dm = dm_ids & precise
    batch_dm = dm_ids & batch
    ignored_dm = dm_ids - precise - batch
    buckets = {name: set() for name in ("low", "mid", "high")}
    for event in events:
        if event["type"] == "danmaku":
            buckets[bucket_for(event, scorebook)].add(event["id"])
    high_ids = buckets["high"]
    high_responded = high_ids & (precise | batch)
    responded_dm = dm_ids & (precise | batch)
    waits = [states[event_id]["t"] - float(event_by_id[event_id]["ts"])
             for event_id in responded_dm]
    high_waits = [states[event_id]["t"] - float(event_by_id[event_id]["ts"])
                  for event_id in high_responded]
    precise_scores = [scorebook[event_by_id[event_id]["text"]] for event_id in precise_dm]
    batch_decisions = [decision for decision in decisions if decision.get("type") == "batch"]
    occupied = math.fsum(float(decision["duration"]) for decision in decisions)
    first_arrival = min(float(event["ts"]) for event in events)
    last_end = max((float(decision["t"]) + float(decision["duration"])
                    for decision in decisions), default=first_arrival)
    span = max(0.0, last_end - first_arrival)
    n_dm = len(dm_ids)
    metrics = {
        "n_danmaku": n_dm,
        "n_gifts": len(gift_ids),
        "precise_n": len(precise_dm),
        "batch_covered_n": len(batch_dm),
        "ignored_n": len(ignored_dm),
        "total_cov": ratio(len(precise_dm) + len(batch_dm), n_dm),
        "precise_rate": ratio(len(precise_dm), n_dm),
        "batch_cov_rate": ratio(len(batch_dm), n_dm),
        "ignore_rate": ratio(len(ignored_dm), n_dm),
        "low_cov": ratio(len(buckets["low"] & (precise | batch)), len(buckets["low"])),
        "mid_cov": ratio(len(buckets["mid"] & (precise | batch)), len(buckets["mid"])),
        "high_cov": ratio(len(high_responded), len(high_ids)),
        "high_precise_rate": ratio(len(high_ids & precise), len(high_ids)),
        "high_wait_s": mean(high_waits),
        "mean_wait_s": mean(waits),
        "mean_precise_score": mean(precise_scores),
        "gift_cov": ratio(len(gift_ids & (precise | batch)), len(gift_ids)),
        "gift_responded_n": len(gift_ids & (precise | batch)),
        "generation_calls": len(decisions),
        "batch_n": len(batch_decisions),
        "batch_avg_size": ratio(len(batch_dm), len(batch_decisions)),
        "occupied_s": occupied,
        "span_s": span,
        "gap_s": max(0.0, span - occupied),
    }
    return metrics, precise, batch, buckets


def add_slots(metrics, decisions, spec, experiment):
    if experiment == "R1":
        precise_duration = r1_precise_duration(spec["scenario"])
        slots = 0
        batch_ks = []
        for decision in decisions:
            if decision.get("type") == "batch":
                n = len(decision["covered"])
                k = math.ceil(r1_batch_duration(spec["scenario"], n) / precise_duration)
                batch_ks.append(k)
                slots += k
            else:
                slots += 1
        metrics["batch_k_min"] = min(batch_ks) if batch_ks else None
        metrics["batch_k_max"] = max(batch_ks) if batch_ks else None
        metrics["batch_k_mean"] = mean(batch_ks) if batch_ks else None
    else:
        slots = sum(math.ceil(float(decision["duration"]) / 7.8) for decision in decisions)
    metrics["equivalent_slots"] = slots
    metrics["per_precise_slot_cov"] = ratio(
        metrics["precise_n"] + metrics["batch_covered_n"], slots, empty=None)


def fano_and_peak(events, window_s):
    n_windows = 1800 // window_s
    counts = [0] * n_windows
    for event in events:
        if event["type"] != "danmaku":
            continue
        timestamp = float(event["ts"])
        if not 0 <= timestamp <= 1800:
            continue
        index = n_windows - 1 if timestamp == 1800 else int(timestamp // window_s)
        counts[index] += 1
    count_mean = math.fsum(counts) / n_windows
    variance = math.fsum((count - count_mean) ** 2 for count in counts) / (n_windows - 1)
    return {
        "fano": variance / count_mean if count_mean else 0.0,
        "peak_per_min": max(counts) * 60 / window_s,
    }


def longest_small_precise_streak(decisions, small_ids):
    longest = current = 0
    ordered = sorted(enumerate(decisions), key=lambda pair: (float(pair[1]["t"]), pair[0]))
    for _, decision in ordered:
        if decision.get("type") != "batch" and decision.get("ev_id") in small_ids:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def build_row(experiment, spec, events, decisions, states, scorebook):
    metrics, precise, batch, _ = common_metrics(events, decisions, states, scorebook)
    if experiment in ("R1", "R2"):
        add_slots(metrics, decisions, spec, experiment)
    if experiment == "R1":
        return {"scenario": spec["scenario"], "lambda_per_min": spec["load"],
                "seed": spec["seed"], "stream": spec["stream"],
                "strategy": spec["strategy"], **metrics}
    if experiment == "R2":
        fano10 = fano_and_peak(events, 10)
        fano1 = fano_and_peak(events, 1)
        peak60 = fano_and_peak(events, 60)
        return {
            "process": spec["process"], "lambda_target_per_min": spec["load"],
            "seed": spec["seed"], "stream": spec["stream"], "strategy": spec["strategy"],
            "n_danmaku": metrics["n_danmaku"], "n_gifts": metrics["n_gifts"],
            "actual_lambda_per_min": metrics["n_danmaku"] / 30.0,
            "fano_10s": fano10["fano"], "fano_1s": fano1["fano"],
            "peak_60s_per_min": peak60["peak_per_min"],
            **{key: value for key, value in metrics.items()
               if key not in ("n_danmaku", "n_gifts", "gift_responded_n", "batch_avg_size",
                              "span_s", "equivalent_slots")},
        }

    base = spec["stream"].split("_s")[0]
    load = int(base.split("_")[1][1:])
    seed = int(spec["stream"].split("_s")[1]) if "_s" in spec["stream"] else 42
    profile = "drift" if base.endswith("drift") else "uniform"
    primary = load in (12, 24) and profile == "drift"
    n_dm = metrics["n_danmaku"]
    precise_score_sum = math.fsum(scorebook[event["text"]] for event in events
                                  if event["id"] in precise and event["type"] == "danmaku")
    batch_score_sum = math.fsum(scorebook[event["text"]] for event in events
                                if event["id"] in batch and event["type"] == "danmaku")
    gifts = [event for event in events if canonical_type(event["type"]) == "gift"]
    small = {event["id"] for event in gifts if float(event["price"]) <= 100}
    high = {event["id"] for event in gifts if float(event["price"]) > 100}
    responded = precise | batch
    row = {
        "analysis_role": "primary" if primary else "descriptive",
        "stream": spec["stream"], "lambda_per_min": load, "profile": profile,
        "seed": seed, "strategy": spec["strategy"],
        **{key: metrics[key] for key in (
            "n_danmaku", "precise_n", "batch_covered_n", "ignored_n", "total_cov",
            "mean_precise_score", "high_precise_rate", "high_wait_s")},
        "small_gift_n": len(small),
        "small_gift_precise_n": len(small & precise),
        "small_gift_responded_n": len(small & responded),
        "small_gift_precise_rate": ratio(len(small & precise), len(small), empty=None),
        "small_gift_response_rate": ratio(len(small & responded), len(small), empty=None),
        "longest_small_precise_streak": longest_small_precise_streak(decisions, small),
        "high_gift_n": len(high),
        "high_gift_precise_n": len(high & precise),
        "high_gift_responded_n": len(high & responded),
    }
    for weight in WEIGHTS:
        row[utility_key(weight)] = ((precise_score_sum + weight * batch_score_sum) / (80 * n_dm)
                                    if n_dm else None)
    return row


def make_specs(experiment, smoke):
    specs = []
    if experiment == "R1":
        for scenario in ("H0", "P1", "P2"):
            for load in ([12] if smoke else [12, 24]):
                for seed in ([42] if smoke else SEEDS):
                    stream = r1_stream(load, seed)
                    for strategy in STRATEGIES:
                        output_dir = RESULTS_DIR / "smoke_calibration" if smoke else RESULTS_DIR
                        specs.append({
                            "scenario": scenario, "load": load, "seed": seed,
                            "stream": stream, "strategy": strategy,
                            "key": (scenario, stream, strategy),
                            "key_fields": ("scenario", "stream", "strategy"),
                            "run_name": f"{scenario}|{stream}|{strategy}",
                            "events_path": DATA_DIR / f"{stream}.jsonl",
                            "decisions_path": output_dir /
                                f"decisions_calibration_{scenario}_{stream}_{strategy}.jsonl",
                        })
    elif experiment == "R2":
        for process in ("poisson", "onoff"):
            for load in ([12] if smoke else [12, 24]):
                for seed in ([42] if smoke else SEEDS):
                    stream = f"onoff_{process}_l{load}_s{seed}"
                    for strategy in STRATEGIES:
                        output_dir = RESULTS_DIR / "smoke_arrival" if smoke else RESULTS_DIR
                        specs.append({
                            "process": process, "load": load, "seed": seed,
                            "stream": stream, "strategy": strategy,
                            "key": (process, stream, strategy),
                            "key_fields": ("process", "stream", "strategy"),
                            "run_name": f"{process}|{stream}|{strategy}",
                            "events_path": DATA_DIR / f"{stream}.jsonl",
                            "decisions_path": output_dir /
                                f"decisions_arrival_{process}_l{load}_s{seed}_{strategy}.jsonl",
                        })
    else:
        for stream in r3_streams():
            for strategy in STRATEGIES:
                specs.append({
                    "stream": stream, "strategy": strategy,
                    "key": (stream, strategy), "key_fields": ("stream", "strategy"),
                    "run_name": f"{stream}|{strategy}",
                    "events_path": DATA_DIR / f"{stream}.jsonl",
                    "decisions_path": RESULTS_DIR / f"decisions_merge_{stream}_{strategy}.jsonl",
                })
    return specs


def add_derived_fields(experiment, rows):
    unavailable = {}
    for key, row in rows.items():
        unavailable[key] = set()
        if experiment in ("R1", "R2"):
            greedy_key = (key[0], key[1], "greedy")
            if greedy_key in rows:
                row["coverage_delta_vs_greedy"] = row["total_cov"] - rows[greedy_key]["total_cov"]
            else:
                unavailable[key].add("coverage_delta_vs_greedy")
        if experiment == "R2":
            if row["process"] == "onoff":
                paired_key = ("poisson", row["stream"].replace("onoff_onoff_", "onoff_poisson_"),
                              row["strategy"])
                if paired_key in rows:
                    row["onoff_minus_poisson_cov"] = row["total_cov"] - rows[paired_key]["total_cov"]
                else:
                    unavailable[key].add("onoff_minus_poisson_cov")
            else:
                row["onoff_minus_poisson_cov"] = None
    return unavailable


def check_csv(report, experiment, specs, fields, csv_path, rows, unavailable):
    loaded = report.read_csv(csv_path, experiment, fields)
    if loaded is None:
        return 0
    headers, actual_rows = loaded
    expected_keys = {spec["key"] for spec in specs}
    key_fields = specs[0]["key_fields"]
    actual_by_key = {}
    ambiguous = set()
    for row_index, actual in enumerate(actual_rows, 2):
        key = tuple(actual.get(field) for field in key_fields)
        if key in actual_by_key:
            report.add(experiment, "|".join(str(part) for part in key), "csv_structure",
                       f"row {row_index}", "unique CSV run key; no de-duplication", key)
            ambiguous.add(key)
        else:
            actual_by_key[key] = actual
        if key not in expected_keys:
            report.add(experiment, "|".join(str(part) for part in key), "unexpected_csv_row",
                       f"row {row_index}", "only expected runs", key)
    checked = 0
    for spec in specs:
        key = spec["key"]
        run_name = spec["run_name"]
        if key in ambiguous:
            continue
        if key not in actual_by_key:
            report.add(experiment, run_name, "missing_csv_row", "run key", key, "missing")
            continue
        if key not in rows:
            continue
        actual = actual_by_key[key]
        for field in fields:
            if field not in headers or field in unavailable.get(key, set()):
                continue
            compare_field(report, experiment, run_name, field, rows[key].get(field), actual.get(field))
        checked += 1
    return checked


def check_experiment(report, experiment, smoke, scorebook):
    specs = make_specs(experiment, smoke)
    rows = {}
    for spec in specs:
        run_name = spec["run_name"]
        events = report.read_jsonl(spec["events_path"], experiment, run_name)
        decisions = report.read_jsonl(spec["decisions_path"], experiment, run_name)
        if events is None or decisions is None:
            continue
        try:
            states = build_states(report, experiment, run_name, events, decisions, scorebook, spec)
            if states is None:
                continue
            rows[spec["key"]] = build_row(
                experiment, spec, events, decisions, states, scorebook)
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            report.add(experiment, run_name, "recompute_error", "run",
                       "independently recomputed", f"{type(exc).__name__}: {exc}")
    unavailable = add_derived_fields(experiment, rows)
    if experiment == "R1":
        csv_path = (RESULTS_DIR / "smoke_calibration" / "calibration_robustness.csv"
                    if smoke else RESULTS_DIR / "calibration_robustness.csv")
        fields = R1_FIELDS
    elif experiment == "R2":
        csv_path = (RESULTS_DIR / "smoke_arrival" / "arrival_robustness.csv"
                    if smoke else RESULTS_DIR / "arrival_robustness.csv")
        fields = R2_FIELDS
    else:
        csv_path = RESULTS_DIR / "objective_robustness.csv"
        fields = R3_FIELDS
    checked = check_csv(report, experiment, specs, fields, csv_path, rows, unavailable)
    report.finish_summary(experiment, len(specs), len(rows), checked)


def markdown_value(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text.replace("|", "\\|").replace("\n", "\\n")


def render_markdown(report):
    total_expected = sum(item["expected_runs"] for item in report.summaries.values())
    total_recomputed = sum(item["recomputed_runs"] for item in report.summaries.values())
    total_checked = sum(item["checked_runs"] for item in report.summaries.values())
    status = "PASS" if not report.differences and total_checked == total_expected else "FAIL"
    lines = [
        "# R1/R2/R3 独立复算报告",
        "",
        f"- 模式：`{report.mode}`",
        f"- 状态：**{status}**",
        f"- 应检查运行：{total_expected}；已复算：{total_recomputed}；已逐字段核对：{total_checked}",
        f"- mismatch 数：{len(report.differences)}（每条差异均列于下文；缺失输入也计 mismatch）",
        f"- 数值容差：绝对 {ABS_TOL:g}，相对 {REL_TOL:g}；整数和文本精确比较",
        "- 未导入实验实现或原指标模块；评分仅取冻结文本池，状态连接禁止静默去重。",
        "",
        "| 实验 | 应检查 | 已复算 | 已核对 | mismatch |",
        "|---|---:|---:|---:|---:|",
    ]
    for experiment in ("R1", "R2", "R3"):
        item = report.summaries.get(experiment, {
            "expected_runs": 0, "recomputed_runs": 0, "checked_runs": 0,
            "mismatch_count": sum(d["experiment"] == experiment for d in report.differences),
        })
        lines.append(f"| {experiment} | {item['expected_runs']} | {item['recomputed_runs']} | "
                     f"{item['checked_runs']} | {item['mismatch_count']} |")
    lines += [
        "",
        "## 固定复算口径",
        "",
        "- 三态按事件 ID 唯一连接；`sc` 与 `gift` 归为礼物，弹幕和礼物分别满足精回+批覆盖+未回=N。",
        "- R1：逐批从 `covered` 取 n；P1/P2 用未取整模型时长计算 k，占用为全部决策 duration 之和。",
        "- R2：实际 λ=N/30；10 s/1 s Fano 使用 1800 s 全部完整非重叠窗（含零窗、ddof=1）；60 s 峰值为最大窗计数。",
        "- R3：仅原 `decisions_merge`；U 分母为 80N，未回为 0；price=100 属小礼物，price=101 属高价值礼物。",
        "- 高分等待覆盖高分精回与高分批覆盖；最长连谢按 `(决策 t, 原文件顺序)` 排序，任何其他回应动作均中断。",
        "",
        "## 全部差异",
        "",
    ]
    if not report.differences:
        lines.append("无差异。")
    else:
        lines += [
            "| # | 实验 | 运行 | 类型 | 字段/位置 | 期望 | 实际 |",
            "|---:|---|---|---|---|---|---|",
        ]
        for index, difference in enumerate(report.differences, 1):
            lines.append(
                f"| {index} | {markdown_value(difference['experiment'])} | "
                f"{markdown_value(difference['run'])} | {markdown_value(difference['kind'])} | "
                f"{markdown_value(difference['field'])} | {markdown_value(difference['expected'])} | "
                f"{markdown_value(difference['actual'])} |")
    lines += ["", "## 输入 SHA-256", ""]
    for path, digest in sorted(report.input_sha256.items()):
        lines.append(f"- `{path}`: `{digest}`")
    return "\n".join(lines) + "\n", status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true",
                        help="check smoke_calibration/smoke_arrival plus full R3")
    args = parser.parse_args(argv)
    report = Report(args.smoke)
    script_path = Path(__file__).resolve()
    report.input_sha256[relative_path(script_path)] = hashlib.sha256(script_path.read_bytes()).hexdigest()

    pool_path = DATA_DIR / "ovl_text_pool.jsonl"
    pool = report.read_jsonl(pool_path, "common", None)
    scorebook = {}
    if pool is not None:
        try:
            for row in pool:
                text = row["text"]
                score = int(row["score"])
                if text in scorebook and scorebook[text] != score:
                    raise ValueError(f"conflicting frozen scores for text: {text}")
                scorebook[text] = score
        except (KeyError, TypeError, ValueError) as exc:
            report.add("common", None, "scorebook_error", relative_path(pool_path),
                       "consistent text/score pairs", str(exc))
            scorebook = {}
    if pool is not None and scorebook:
        for experiment in ("R1", "R2", "R3"):
            check_experiment(report, experiment, args.smoke, scorebook)

    payload, status = render_markdown(report)
    document = {
        "mode": report.mode,
        "status": status,
        "expected_runs": sum(item["expected_runs"] for item in report.summaries.values()),
        "recomputed_runs": sum(item["recomputed_runs"] for item in report.summaries.values()),
        "checked_runs": sum(item["checked_runs"] for item in report.summaries.values()),
        "mismatch_count": len(report.differences),
        "tolerance": {"absolute": ABS_TOL, "relative": REL_TOL},
        "experiments": report.summaries,
        "differences": report.differences,
        "input_sha256": dict(sorted(report.input_sha256.items())),
    }
    json_path = RESULTS_DIR / "robustness_recalculation.json"
    md_path = RESULTS_DIR / "robustness_recalculation.md"
    with json_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        handle.write("\n")
    with md_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
    print(f"{status}: checked={document['checked_runs']}/{document['expected_runs']}, "
          f"mismatches={document['mismatch_count']}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0 if status == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
