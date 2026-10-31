#!/usr/bin/env python3
"""Validate a completed benchmark CSV without modifying it."""

import argparse
import csv
import math
import sys
from pathlib import Path


CSV_FIELDS = (
    "timestamp_epoch", "engine", "profile", "query_id", "concurrency", "run_number",
    "throughput_qps", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
    "cpu_throttled_seconds", "max_rss_mb", "disk_spill", "status",
)
ENGINES = ("mongodb", "postgresql")
PROFILES = ("PROFILE_A_BASELINE", "PROFILE_B_MEDIUM", "PROFILE_C_SEVERE")
QUERIES = ("Q1", "Q2")
CONCURRENCIES = ("1", "10", "50")
RUN_NUMBERS = ("1", "2", "3")
STATUSES = ("ok", "spill", "oom", "error")
METRIC_FIELDS = (
    "throughput_qps", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
    "cpu_throttled_seconds", "max_rss_mb",
)
LATENCY_FIELDS = ("latency_p50_ms", "latency_p95_ms", "latency_p99_ms")
EXPECTED_KEYS = {
    (engine, profile, query, concurrency, run_number)
    for engine in ENGINES
    for profile in PROFILES
    for query in QUERIES
    for concurrency in CONCURRENCIES
    for run_number in RUN_NUMBERS
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="completed benchmark CSV to validate")
    return parser.parse_args()


def parse_number(value, field, line_number, errors):
    if value == "":
        return None
    try:
        number = float(value)
    except ValueError:
        errors.append(f"line {line_number}: {field} must be numeric or blank")
        return None
    if not math.isfinite(number):
        errors.append(f"line {line_number}: {field} must be finite")
        return None
    if number < 0:
        errors.append(f"line {line_number}: {field} cannot be negative")
        return None
    return number


def validate_timestamp(value, line_number, errors):
    try:
        timestamp = int(value)
    except ValueError:
        errors.append(f"line {line_number}: timestamp_epoch must be an integer")
        return
    if timestamp <= 0 or str(timestamp) != value:
        errors.append(f"line {line_number}: timestamp_epoch must be a positive integer")


def row_key(row):
    return (
        row["engine"], row["profile"], row["query_id"],
        row["concurrency"], row["run_number"],
    )


def validate_row(row, line_number, errors):
    validate_timestamp(row["timestamp_epoch"], line_number, errors)
    dimensions = {
        "engine": ENGINES,
        "profile": PROFILES,
        "query_id": QUERIES,
        "concurrency": CONCURRENCIES,
        "run_number": RUN_NUMBERS,
        "status": STATUSES,
    }
    for field, allowed in dimensions.items():
        if row[field] not in allowed:
            errors.append(f"line {line_number}: {field} has an unsupported value")

    numbers = {
        field: parse_number(row[field], field, line_number, errors)
        for field in METRIC_FIELDS
    }
    spill = row["disk_spill"]
    if spill not in ("", "true", "false"):
        errors.append(f"line {line_number}: disk_spill must be true, false, or blank")

    throughput = numbers["throughput_qps"]
    latencies = [numbers[field] for field in LATENCY_FIELDS]
    if throughput is None:
        if any(row[field] != "" for field in METRIC_FIELDS[1:]) or spill != "":
            errors.append(f"line {line_number}: blank throughput requires all other metrics blank")
    elif throughput == 0:
        if any(value is not None for value in latencies):
            errors.append(f"line {line_number}: zero throughput requires blank latency percentiles")
    elif any(value is None for value in latencies):
        errors.append(f"line {line_number}: positive throughput requires all latency percentiles")

    if all(value is not None for value in latencies):
        if not (latencies[0] <= latencies[1] <= latencies[2]):
            errors.append(f"line {line_number}: latency percentiles must be ordered P50 <= P95 <= P99")

    status = row["status"]
    if status == "ok":
        for field in ("throughput_qps", "cpu_throttled_seconds", "max_rss_mb"):
            if numbers[field] is None:
                errors.append(f"line {line_number}: status {status} requires numeric {field}")
        if spill == "":
            errors.append(f"line {line_number}: status {status} requires observed disk_spill")
        if spill != "false":
            errors.append(f"line {line_number}: status ok requires disk_spill=false")
    elif status == "spill":
        if numbers["throughput_qps"] is None:
            errors.append(f"line {line_number}: status spill requires numeric throughput_qps")
        if spill != "true":
            errors.append(f"line {line_number}: status spill requires disk_spill=true")


def validate_csv(path):
    errors = []
    if not path.is_file():
        return [f"CSV file does not exist or is not a regular file: {path}"], 0

    row_count = 0
    seen = set()
    try:
        with path.open("r", newline="", encoding="utf-8") as stream:
            reader = csv.reader(stream, strict=True)
            header = next(reader, None)
            if header != list(CSV_FIELDS):
                return ["CSV header does not exactly match the required schema"], 0
            for line_number, values in enumerate(reader, start=2):
                row_count += 1
                if len(values) != len(CSV_FIELDS):
                    errors.append(f"line {line_number}: expected {len(CSV_FIELDS)} fields, found {len(values)}")
                    continue
                row = dict(zip(CSV_FIELDS, values))
                validate_row(row, line_number, errors)
                key = row_key(row)
                if key in seen:
                    errors.append(f"line {line_number}: duplicate matrix/repetition key {key}")
                seen.add(key)
    except (OSError, UnicodeError, csv.Error, StopIteration) as exc:
        return [f"could not parse CSV: {type(exc).__name__}"], row_count

    if row_count != 108:
        errors.append(f"expected exactly 108 data rows, found {row_count}")
    missing = EXPECTED_KEYS - seen
    unexpected = seen - EXPECTED_KEYS
    if missing:
        errors.append(f"missing {len(missing)} expected matrix/repetition keys")
    if unexpected:
        errors.append(f"found {len(unexpected)} unsupported matrix/repetition keys")
    return errors, row_count


def main():
    args = parse_args()
    errors, row_count = validate_csv(args.csv)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"Valid CSV: {row_count} rows cover all 108 matrix/repetition keys.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
