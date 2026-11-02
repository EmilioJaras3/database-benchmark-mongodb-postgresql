#!/usr/bin/env python3
"""Run three measured repetitions for one benchmark matrix cell."""

import argparse
import csv
import json
import math
import multiprocessing
import os
import re
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import check_spec


ROOT = Path(__file__).resolve().parent
QUERIES = ROOT / "queries"
CSV_FIELDS = (
    "timestamp_epoch", "engine", "profile", "query_id", "concurrency", "run_number",
    "throughput_qps", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
    "cpu_throttled_seconds", "max_rss_mb", "disk_spill", "status",
)
WARMUP_SECONDS = 60
MEASURE_SECONDS = 300
COOLDOWN_SECONDS = 60
REPETITIONS = 3
CONCURRENCIES = (1, 10, 50)
PROFILES = {
    "PROFILE_A_BASELINE": "a",
    "PROFILE_B_MEDIUM": "b",
    "PROFILE_C_SEVERE": "c",
}


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, type=Path, help="caller-supplied CSV output path")
    parser.add_argument("--engine", required=True, choices=("mongodb", "postgresql"))
    parser.add_argument("--profile", required=True, choices=tuple(PROFILES))
    parser.add_argument("--query", required=True, choices=("Q1", "Q2"))
    parser.add_argument("--concurrency", required=True, type=int, choices=CONCURRENCIES)
    parser.add_argument(
        "--resume", action="store_true",
        help="validate an existing partial CSV and skip matrix/repetition keys already recorded",
    )
    args = parser.parse_args()
    if args.engine == "postgresql" and postgres_dsn_from_env() is None:
        parser.error("Set POSTGRES_DSN or POSTGRES_PASSWORD in the process environment")
    return args


def postgres_dsn_from_env(environ=None):
    """Return an explicit PostgreSQL DSN or build the benchmark-local default."""
    environment = os.environ if environ is None else environ
    explicit_dsn = environment.get("POSTGRES_DSN")
    if explicit_dsn:
        return explicit_dsn

    password = environment.get("POSTGRES_PASSWORD")
    if not password:
        return None

    encoded_password = quote(password, safe="")
    return f"postgresql://postgres:{encoded_password}@127.0.0.1:5434/postgres"


def read_mongodb_pipeline(query_id):
    source = (QUERIES / f"mongodb_{query_id.lower()}.js").read_text(encoding="utf-8")
    start = source.find("[")
    end = source.rfind("]")
    if start < 0 or end < start:
        raise ValueError(f"MongoDB {query_id} query file has no pipeline array")
    return json.loads(source[start : end + 1])


def read_postgresql_query(query_id):
    source = (QUERIES / f"postgresql_{query_id.lower()}.sql").read_text(encoding="utf-8").strip()
    statements = [statement.strip() for statement in source.split(";") if statement.strip()]
    settings = [statement for statement in statements if statement.upper().startswith("SET ")]
    selects = [statement for statement in statements if not statement.upper().startswith("SET ")]
    if len(selects) != 1:
        raise ValueError(f"PostgreSQL {query_id} query file must contain one SELECT")
    return settings, selects[0]


def classify_error(exc):
    message = str(exc).lower()
    if isinstance(exc, MemoryError) or any(
        marker in message for marker in ("out of memory", "memory limit", "exceeded memory", "oom")
    ):
        return "oom"
    if getattr(exc, "code", None) == 292:
        return "oom"
    return "error"


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def run_worker(worker_id, engine, uri, query_id, pipeline, pg_settings, pg_statement,
               marker, start_event, measuring_event, stop_event, deadline, events):
    client = None
    connection = None
    backend_pid = None
    try:
        if engine == "mongodb":
            from pymongo import MongoClient

            client = MongoClient(uri, appname="database-benchmark", connectTimeoutMS=10_000)
            client.admin.command("ping")
            database = client.get_default_database(default="test")
            collection = database["orders"]
        else:
            import psycopg

            connection = psycopg.connect(uri, connect_timeout=10, autocommit=True)
            connection.execute("SELECT set_config('application_name', %s, false)", (marker,)).fetchone()
            backend_pid = connection.execute("SELECT pg_backend_pid()").fetchone()[0]
            for statement in pg_settings:
                connection.execute(statement)

        events.put(("ready", worker_id, "ok", backend_pid))
        start_event.wait()
        latencies = []
        completed = 0
        worker_status = "ok"
        reason_code = ""
        measured_query_ids = []
        query_number = 0
        while not stop_event.is_set():
            is_measuring = measuring_event.is_set()
            if is_measuring:
                with deadline.get_lock():
                    measure_deadline = deadline.value
                if time.monotonic() >= measure_deadline:
                    break

            query_started = time.perf_counter()
            try:
                if engine == "mongodb":
                    options = {"comment": marker}
                    if is_measuring:
                        query_number += 1
                        measured_comment_id = f"{marker}-{worker_id}-{query_number}"
                        measured_query_ids.append(measured_comment_id)
                        options["comment"] = measured_comment_id
                    if query_id == "Q2":
                        options["allowDiskUse"] = False
                    list(collection.aggregate(pipeline, **options))
                else:
                    cursor = connection.execute(pg_statement)
                    cursor.fetchall()
            except Exception as exc:
                worker_status = classify_error(exc)
                if worker_status == "error":
                    reason_code = "worker_query_failed"
                break

            elapsed_ms = (time.perf_counter() - query_started) * 1000
            if is_measuring:
                if time.monotonic() > measure_deadline:
                    break
                latencies.append(elapsed_ms)
                completed += 1

        events.put(("result", worker_id, completed, latencies, worker_status, backend_pid,
                    reason_code, measured_query_ids))
    except Exception as exc:
        worker_status = classify_error(exc)
        reason_code = "worker_startup_failed" if worker_status == "error" else ""
        events.put(("ready", worker_id, worker_status, backend_pid, reason_code))
    finally:
        if client is not None:
            client.close()
        if connection is not None:
            connection.close()


def wait_for_ready(events, workers, timeout=30):
    replies = []
    for _ in workers:
        replies.append(events.get(timeout=timeout))
    return replies


def resolve_compose_container_id(service):
    """Return the full ID for exactly one Compose service container, or None."""
    try:
        result = subprocess.run(
            ["docker", "compose", "ps", "--quiet", service],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    container_ids = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if len(container_ids) != 1 or not re.fullmatch(r"[0-9a-f]{64}", container_ids[0]):
        return None
    return container_ids[0]


def query_prometheus_range(prometheus_url, metric, container_id, start_epoch, end_epoch):
    selector = f'{metric}{{id="/docker/{container_id}"}}'
    params = urllib.parse.urlencode(
        {"query": selector, "start": start_epoch, "end": end_epoch, "step": "1s"}
    )
    request = urllib.request.Request(
        f"{prometheus_url.rstrip('/')}/api/v1/query_range?{params}",
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError("Prometheus query failed")
    series = payload.get("data", {}).get("result", [])
    if not series:
        return []
    if len(series) != 1 or series[0].get("metric", {}).get("id") != f"/docker/{container_id}":
        raise RuntimeError("Prometheus returned an ambiguous or mismatched container series")
    samples = []
    for timestamp, value in series[0].get("values", []):
        number = float(value)
        if math.isfinite(number):
            samples.append((timestamp, number))
    return samples


def read_telemetry(prometheus_url, engine, profile, start_epoch):
    suffix = PROFILES[profile]
    service_engine = "mongo" if engine == "mongodb" else "postgres"
    service = f"{service_engine}-profile-{suffix}"
    end_epoch = start_epoch + MEASURE_SECONDS
    cpu = None
    rss = None
    container_id = resolve_compose_container_id(service)
    if container_id is None:
        return None, None
    try:
        cpu_samples = query_prometheus_range(
            prometheus_url, "container_cpu_cfs_throttled_seconds_total", container_id,
            start_epoch, end_epoch,
        )
        if len(cpu_samples) >= 2:
            delta = cpu_samples[-1][1] - cpu_samples[0][1]
            if delta >= 0:
                cpu = delta
        rss_samples = query_prometheus_range(
            prometheus_url, "container_memory_rss", container_id, start_epoch, end_epoch,
        )
        if rss_samples:
            rss = max(value for _, value in rss_samples) / (1024 * 1024)
    except (OSError, ValueError, urllib.error.URLError, RuntimeError, json.JSONDecodeError):
        return None, None
    return cpu, rss


def nested_key(value, key):
    if isinstance(value, dict):
        if key in value and isinstance(value[key], bool):
            yield value[key]
        for nested in value.values():
            yield from nested_key(nested, key)
    elif isinstance(value, list):
        for nested in value:
            yield from nested_key(nested, key)


def nested_values(value, key):
    if isinstance(value, dict):
        if key in value:
            yield value[key]
        for nested in value.values():
            yield from nested_values(nested, key)
    elif isinstance(value, list):
        for nested in value:
            yield from nested_values(nested, key)


def mongodb_disk_spill(service, expected_query_ids, start_epoch):
    expected = set(expected_query_ids)
    if not expected:
        return None
    since = datetime.fromtimestamp(start_epoch, timezone.utc).isoformat()
    try:
        result = subprocess.run(
            ["docker", "compose", "logs", "--no-color", "--since", since, service],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            return None
        malformed_match = False
        completed = set()
        positive_spill = False
        for line in (result.stdout or "").splitlines():
            if not any(query_id in line for query_id in expected):
                continue
            json_start = line.find("{")
            if json_start < 0:
                malformed_match = True
                continue
            try:
                entry = json.loads(line[json_start:])
            except (TypeError, json.JSONDecodeError):
                malformed_match = True
                continue
            if not isinstance(entry, dict):
                malformed_match = True
                continue
            comments = (comment for comment in nested_values(entry, "comment")
                       if isinstance(comment, str))
            matching_ids = expected.intersection(comments)
            if not matching_ids:
                continue
            if entry.get("msg") != "Slow query":
                continue
            completed.update(matching_ids)
            positive_spill = positive_spill or any(
                value is True for value in nested_key(entry, "usedDisk")
            )
        if malformed_match:
            return None
        if positive_spill:
            return True
        if expected.issubset(completed):
            return False
        return None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def postgresql_disk_spill(service, pids, start_epoch):
    if not pids:
        return None
    since = datetime.fromtimestamp(start_epoch, timezone.utc).isoformat()
    try:
        result = subprocess.run(
            ["docker", "compose", "logs", "--no-color", "--since", since, service],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    pid_pattern = re.compile(r"\[(\d+)\]")
    temp_pattern = re.compile(r"temporary file:", re.IGNORECASE)
    relevant_lines = []
    for line in result.stdout.splitlines():
        pid_match = pid_pattern.search(line)
        if pid_match and int(pid_match.group(1)) in pids:
            relevant_lines.append(line)
    return any(temp_pattern.search(line) for line in relevant_lines)


def stop_workers(workers, grace_seconds):
    deadline = time.monotonic() + grace_seconds
    for worker in workers:
        worker.join(timeout=max(0, deadline - time.monotonic()))

    forced = [worker for worker in workers if worker.is_alive()]
    for worker in forced:
        worker.terminate()

    termination_deadline = time.monotonic() + 1
    for worker in forced:
        worker.join(timeout=max(0, termination_deadline - time.monotonic()))

    return bool(forced), any(worker.is_alive() for worker in forced)


def append_row(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    needs_header = not path.exists() or path.stat().st_size == 0
    if not needs_header:
        with path.open("r", newline="", encoding="utf-8") as stream:
            existing_header = next(csv.reader(stream), None)
        if existing_header != list(CSV_FIELDS):
            raise ValueError("existing CSV has a different header")
    with path.open("a", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
        if needs_header:
            writer.writeheader()
        writer.writerow(row)


def flush_wsl_cache(run_number, environ=None):
    """Flush recoverable WSL2 caches, failing closed without disclosing environment data."""
    environment = os.environ if environ is None else environ
    distribution = environment.get("WSL_DISTRO_NAME")
    if not distribution:
        raise SystemExit("WSL2 cache flush failed; stopping before measurement.")
    try:
        result = subprocess.run(
            [
                "wsl.exe", "--distribution", distribution, "--user", "root", "--exec",
                "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:
        raise SystemExit("WSL2 cache flush failed; stopping before measurement.") from None
    if result.returncode != 0:
        raise SystemExit("WSL2 cache flush failed; stopping before measurement.")
    print(f"WSL2 cache flush completed before repetition {run_number}/{REPETITIONS}.")


def metric_text(value, digits=6):
    return "" if value is None else f"{value:.{digits}f}"


def failed_row(args, run_number, status, timestamp):
    return {
        "timestamp_epoch": int(timestamp), "engine": args.engine, "profile": args.profile,
        "query_id": args.query, "concurrency": args.concurrency, "run_number": run_number,
        "throughput_qps": "", "latency_p50_ms": "", "latency_p95_ms": "",
        "latency_p99_ms": "", "cpu_throttled_seconds": "", "max_rss_mb": "",
        "disk_spill": "", "status": status,
    }


def error_reason_codes(worker_reasons=(), missing_worker_results=False, forced_shutdown=False,
                       unreaped=False, spill=False, cpu=False, rss=False):
    """Return stable, non-sensitive reason codes for an error repetition."""
    allowed = {
        "measurement_deadline_exceeded", "worker_query_failed", "worker_startup_failed",
        "worker_readiness_timeout", "worker_result_missing", "worker_forced_shutdown",
        "worker_unreaped",
    }
    reasons = set(worker_reasons).intersection(allowed)
    if missing_worker_results:
        reasons.add("worker_result_missing")
    if forced_shutdown:
        reasons.add("worker_forced_shutdown")
    if unreaped:
        reasons.add("worker_unreaped")
    if spill is None:
        reasons.add("spill_evidence_unknown")
    if cpu is None:
        reasons.add("cpu_telemetry_missing")
    if rss is None:
        reasons.add("rss_telemetry_missing")
    return tuple(sorted(reasons))


def emit_error_diagnostic(status, run_number, reason_codes):
    if status != "error":
        return
    reasons = ",".join(reason_codes) or "error_reason_unavailable"
    print(f"run={run_number} status=error reason_codes={reasons}", file=sys.stderr)


def run_repetition(args, run_number, invocation_id, pipeline, pg_settings, pg_statement, uri, prometheus_url):
    context = multiprocessing.get_context("spawn")
    events = context.Queue()
    start_event = context.Event()
    measuring_event = context.Event()
    stop_event = context.Event()
    deadline = context.Value("d", 0.0)
    marker = f"bench-{invocation_id}-{args.profile}-{args.query}-{args.concurrency}-{run_number}"
    workers = [
        context.Process(
            target=run_worker,
            args=(worker_id, args.engine, uri, args.query, pipeline, pg_settings, pg_statement,
                  marker, start_event, measuring_event, stop_event, deadline, events),
            daemon=True,
        )
        for worker_id in range(args.concurrency)
    ]
    for worker in workers:
        worker.start()

    started_at = time.time()
    try:
        ready = wait_for_ready(events, workers)
    except Exception:
        stop_event.set()
        start_event.set()
        forced_shutdown, unreaped = stop_workers(workers, COOLDOWN_SECONDS)
        reasons = error_reason_codes(
            ["worker_readiness_timeout"], forced_shutdown=forced_shutdown, unreaped=unreaped
        )
        emit_error_diagnostic("error", run_number, reasons)
        return failed_row(args, run_number, "error", started_at), None, None, unreaped

    startup_errors = [reply[2] for reply in ready if reply[2] != "ok"]
    if startup_errors:
        stop_event.set()
        start_event.set()
        forced_shutdown, unreaped = stop_workers(workers, COOLDOWN_SECONDS)
        status = "oom" if "oom" in startup_errors else "error"
        reasons = [reply[4] for reply in ready if len(reply) > 4 and reply[4]]
        reasons = reasons or ["worker_startup_failed"]
        reasons = error_reason_codes(
            reasons, forced_shutdown=forced_shutdown, unreaped=unreaped
        )
        emit_error_diagnostic(status, run_number, reasons)
        return failed_row(args, run_number, status, started_at), None, None, unreaped

    backend_pids = {reply[3] for reply in ready if reply[3] is not None}
    start_event.set()
    time.sleep(WARMUP_SECONDS)
    measure_started = time.time()
    with deadline.get_lock():
        deadline.value = time.monotonic() + MEASURE_SECONDS
    measuring_event.set()
    time.sleep(MEASURE_SECONDS)
    stop_event.set()
    forced_shutdown, unreaped = stop_workers(workers, COOLDOWN_SECONDS)
    measure_ended = measure_started + MEASURE_SECONDS
    time.sleep(COOLDOWN_SECONDS)

    results = []
    missing_worker_results = False
    if forced_shutdown or unreaped:
        # A force-killed multiprocessing.Queue writer can leave a partial frame;
        # never read that shared queue after a worker failed to shut down cleanly.
        missing_worker_results = True
        results = [
            ("result", -1, 0, [], "error", None, "worker_result_missing")
            for _ in workers
        ]
    else:
        for _ in workers:
            try:
                results.append(events.get(timeout=2))
            except Exception:
                missing_worker_results = True
                results.append(("result", -1, 0, [], "error", None, "worker_result_missing"))
    completed = sum(result[2] for result in results)
    latencies = [latency for result in results for latency in result[3]]
    worker_statuses = [result[4] for result in results]
    worker_reasons = [result[6] for result in results if len(result) > 6 and result[6]]
    measured_query_ids = [query_id for result in results if len(result) > 7
                          for query_id in result[7]]
    if forced_shutdown or unreaped:
        worker_statuses.append("error")
    if forced_shutdown:
        worker_reasons.append("worker_forced_shutdown")
    if unreaped:
        worker_reasons.append("worker_unreaped")
    status = "oom" if "oom" in worker_statuses else ("error" if "error" in worker_statuses else "ok")

    cpu, rss = read_telemetry(prometheus_url, args.engine, args.profile, measure_started)
    if args.engine == "mongodb":
        suffix = PROFILES[args.profile]
        spill = mongodb_disk_spill(
            f"mongo-profile-{suffix}", measured_query_ids, measure_started
        )
    else:
        suffix = PROFILES[args.profile]
        spill = postgresql_disk_spill(f"postgres-profile-{suffix}", backend_pids, measure_started)
    if status == "ok" and spill is True:
        status = "spill"
    elif status == "ok" and (spill is None or cpu is None or rss is None):
        status = "error"

    if status == "error":
        reasons = error_reason_codes(
            worker_reasons,
            missing_worker_results=missing_worker_results,
            forced_shutdown=forced_shutdown,
            unreaped=unreaped,
            spill=spill,
            cpu=cpu,
            rss=rss,
        )
        emit_error_diagnostic(status, run_number, reasons)

    row = {
        "timestamp_epoch": int(measure_started), "engine": args.engine, "profile": args.profile,
        "query_id": args.query, "concurrency": args.concurrency, "run_number": run_number,
        "throughput_qps": metric_text(completed / MEASURE_SECONDS),
        "latency_p50_ms": metric_text(percentile(latencies, 0.50), 3),
        "latency_p95_ms": metric_text(percentile(latencies, 0.95), 3),
        "latency_p99_ms": metric_text(percentile(latencies, 0.99), 3),
        "cpu_throttled_seconds": metric_text(cpu),
        "max_rss_mb": metric_text(rss, 3),
        "disk_spill": "" if spill is None else str(spill).lower(),
        "status": status,
    }
    return row, measure_started, measure_ended, unreaped


def main():
    args = parse_args()
    existing_keys = set()
    if getattr(args, "resume", False):
        errors, _, existing_keys = check_spec.validate_partial_csv(args.csv)
        if errors:
            raise SystemExit("Resume CSV validation failed: " + "; ".join(errors))
    pipeline = read_mongodb_pipeline(args.query) if args.engine == "mongodb" else None
    pg_settings, pg_statement = (
        read_postgresql_query(args.query) if args.engine == "postgresql" else ([], None)
    )
    uri = (
        os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27017")
        if args.engine == "mongodb"
        else postgres_dsn_from_env()
    )
    prometheus_url = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9091")
    invocation_id = uuid.uuid4().hex
    for run_number in range(1, REPETITIONS + 1):
        key = (
            args.engine, args.profile, args.query, str(args.concurrency), str(run_number),
        )
        if key in existing_keys:
            print(
                f"{args.engine} {args.profile} {args.query} concurrency={args.concurrency} "
                f"run={run_number}: already recorded; skipping"
            )
            continue
        flush_wsl_cache(run_number)
        row, _, _, unreaped = run_repetition(
            args, run_number, invocation_id, pipeline, pg_settings, pg_statement, uri, prometheus_url,
        )
        append_row(args.csv, row)
        existing_keys.add(key)
        print(f"{args.engine} {args.profile} {args.query} concurrency={args.concurrency} run={run_number}: {row['status']}")
        if unreaped:
            raise SystemExit("A benchmark worker remained alive after termination; stopping this scenario.")


if __name__ == "__main__":
    main()
