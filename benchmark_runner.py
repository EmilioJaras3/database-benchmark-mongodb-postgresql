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
    args = parser.parse_args()
    if args.engine == "postgresql" and not os.environ.get("POSTGRES_DSN"):
        parser.error("POSTGRES_DSN must be set in the process environment")
    return args


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
                    if query_id == "Q2":
                        options["allowDiskUse"] = False
                    list(collection.aggregate(pipeline, **options))
                else:
                    cursor = connection.execute(pg_statement)
                    cursor.fetchall()
            except Exception as exc:
                worker_status = classify_error(exc)
                break

            elapsed_ms = (time.perf_counter() - query_started) * 1000
            if is_measuring:
                if time.monotonic() > measure_deadline:
                    worker_status = "error"
                    break
                latencies.append(elapsed_ms)
                completed += 1

        events.put(("result", worker_id, completed, latencies, worker_status, backend_pid))
    except Exception as exc:
        events.put(("ready", worker_id, classify_error(exc), backend_pid))
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


def query_prometheus_range(prometheus_url, metric, service, start_epoch, end_epoch):
    selector = f'{metric}{{container_label_com_docker_compose_service="{service}"}}'
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
    by_timestamp = {}
    for series in payload.get("data", {}).get("result", []):
        for timestamp, value in series.get("values", []):
            number = float(value)
            if math.isfinite(number):
                by_timestamp[timestamp] = by_timestamp.get(timestamp, 0.0) + number
    return sorted(by_timestamp.items())


def read_telemetry(prometheus_url, engine, profile, start_epoch):
    suffix = PROFILES[profile]
    service_engine = "mongo" if engine == "mongodb" else "postgres"
    service = f"{service_engine}-profile-{suffix}"
    end_epoch = start_epoch + MEASURE_SECONDS
    cpu = None
    rss = None
    try:
        cpu_samples = query_prometheus_range(
            prometheus_url, "container_cpu_cfs_throttled_seconds_total", service,
            start_epoch, end_epoch,
        )
        if len(cpu_samples) >= 2:
            delta = cpu_samples[-1][1] - cpu_samples[0][1]
            if delta >= 0:
                cpu = delta
        rss_samples = query_prometheus_range(
            prometheus_url, "container_memory_rss_bytes", service, start_epoch, end_epoch,
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


def mongodb_disk_spill(uri, marker):
    try:
        from pymongo import MongoClient

        with MongoClient(uri, appname="database-benchmark", connectTimeoutMS=10_000) as client:
            lines = client.admin.command({"getLog": "global"}).get("log", [])
        found = []
        for line in lines:
            if marker not in line:
                continue
            try:
                entry = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            found.extend(nested_key(entry, "usedDisk"))
        if not found:
            return None
        return any(found)
    except Exception:
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


def wait_for_manual_cache_flush(run_number):
    message = (
        f"Before repetition {run_number}/{REPETITIONS}, manually flush the cache "
        "according to the playbook, then press Enter to continue. "
        "The runner will not execute the flush command. "
    )
    try:
        input(message)
    except EOFError as exc:
        raise SystemExit("An interactive terminal is required for manual cache flushing.") from exc


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
        _, unreaped = stop_workers(workers, COOLDOWN_SECONDS)
        return failed_row(args, run_number, "error", started_at), None, None, unreaped

    startup_errors = [reply[2] for reply in ready if reply[2] != "ok"]
    if startup_errors:
        stop_event.set()
        start_event.set()
        _, unreaped = stop_workers(workers, COOLDOWN_SECONDS)
        return failed_row(args, run_number, "oom" if "oom" in startup_errors else "error", started_at), None, None, unreaped

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
    for _ in workers:
        try:
            results.append(events.get(timeout=2))
        except Exception:
            results.append(("result", -1, 0, [], "error", None))
    completed = sum(result[2] for result in results)
    latencies = [latency for result in results for latency in result[3]]
    worker_statuses = [result[4] for result in results]
    if forced_shutdown or unreaped:
        worker_statuses.append("error")
    status = "oom" if "oom" in worker_statuses else ("error" if "error" in worker_statuses else "ok")

    cpu, rss = read_telemetry(prometheus_url, args.engine, args.profile, measure_started)
    if args.engine == "mongodb":
        spill = mongodb_disk_spill(uri, marker)
    else:
        suffix = PROFILES[args.profile]
        spill = postgresql_disk_spill(f"postgres-profile-{suffix}", backend_pids, measure_started)
    if status == "ok" and spill is True:
        status = "spill"
    elif status == "ok" and (spill is None or cpu is None or rss is None):
        status = "error"

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
    pipeline = read_mongodb_pipeline(args.query) if args.engine == "mongodb" else None
    pg_settings, pg_statement = (
        read_postgresql_query(args.query) if args.engine == "postgresql" else ([], None)
    )
    uri = os.environ.get("MONGODB_URI", "mongodb://127.0.0.1:27017") if args.engine == "mongodb" else os.environ["POSTGRES_DSN"]
    prometheus_url = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090")
    invocation_id = uuid.uuid4().hex
    for run_number in range(1, REPETITIONS + 1):
        wait_for_manual_cache_flush(run_number)
        row, _, _, unreaped = run_repetition(
            args, run_number, invocation_id, pipeline, pg_settings, pg_statement, uri, prometheus_url,
        )
        append_row(args.csv, row)
        print(f"{args.engine} {args.profile} {args.query} concurrency={args.concurrency} run={run_number}: {row['status']}")
        if unreaped:
            raise SystemExit("A benchmark worker remained alive after termination; stopping this scenario.")


if __name__ == "__main__":
    main()
