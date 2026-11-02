#!/usr/bin/env python3
"""Load the existing generated order array into one benchmark PostgreSQL profile."""

import argparse
import csv
import io
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from load_mongodb_data import DatasetError, JsonArrayStream, validate_document


EXPECTED_RECORDS = 500_000
DEFAULT_DATASET = Path("dataset/orders.json")
PROJECT_NAME = "database-benchmark"
COMPOSE_FILE = Path(__file__).resolve().with_name("docker-compose.yml")
SERVICES = {
    "PROFILE_A_BASELINE": "postgres-profile-a",
    "PROFILE_B_MEDIUM": "postgres-profile-b",
    "PROFILE_C_SEVERE": "postgres-profile-c",
}


class LoaderError(RuntimeError):
    """Raised when the requested import cannot proceed safely."""


def service_for_profile(profile):
    try:
        return SERVICES[profile]
    except KeyError as exc:
        raise LoaderError("Refusing unknown benchmark PostgreSQL profile.") from exc


def docker_compose_base(profile):
    return [
        "docker", "compose", "--project-name", PROJECT_NAME,
        "--file", str(COMPOSE_FILE), "--profile", profile,
    ]


def resolve_container(profile):
    service = service_for_profile(profile)
    result = subprocess.run(
        docker_compose_base(profile) + ["ps", "--all", "--quiet", service],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise LoaderError("Unable to resolve the benchmark PostgreSQL service.")
    container_ids = result.stdout.splitlines()
    if len(container_ids) != 1 or not container_ids[0].strip():
        raise LoaderError("Expected exactly one container for the selected benchmark profile.")
    container_id = container_ids[0].strip()
    inspected = subprocess.run(
        ["docker", "inspect", "--format", "{{json .Config.Labels}}", container_id],
        capture_output=True, text=True, check=False,
    )
    if inspected.returncode != 0:
        raise LoaderError("Unable to verify the selected Compose container.")
    try:
        labels = json.loads(inspected.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LoaderError("Selected container has unreadable Compose labels.") from exc
    if (labels or {}).get("com.docker.compose.project") != PROJECT_NAME or (
        labels or {}
    ).get("com.docker.compose.service") != service:
        raise LoaderError("Refusing a container outside the exact benchmark Compose service.")
    return container_id


def psql_command(container_id):
    return [
        "docker", "exec", "-i", container_id, "psql", "--no-psqlrc",
        "--username", "postgres", "--dbname", "postgres", "--quiet",
        "--set=ON_ERROR_STOP=1",
    ]


def probe_target(container_id):
    result = subprocess.run(
        psql_command(container_id) + ["--tuples-only", "--no-align", "--command",
                                     "SELECT to_regclass('public.orders') IS NOT NULL;"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise LoaderError("Unable to inspect the benchmark PostgreSQL target table.")
    answer = result.stdout.strip()
    if answer not in {"t", "f"}:
        raise LoaderError("Target table inspection returned an unexpected result.")
    return answer == "t"


def ensure_target_is_empty(container_id):
    if probe_target(container_id):
        raise LoaderError("Refusing import: public.orders already exists; no rows were changed.")


def copy_script():
    return (
        "BEGIN;\n"
        "CREATE TABLE public.orders (id UUID PRIMARY KEY, data JSONB);\n"
        "CREATE INDEX orders_timestamp_text_idx ON public.orders USING btree "
        "((data->>'timestamp' COLLATE \"C\"));\n"
        "COPY public.orders (id, data) FROM STDIN WITH (FORMAT csv);\n"
    )


def copy_script_suffix():
    return (
        "\\.\n"
        "\\if :ERROR\n"
        "ROLLBACK;\n"
        "\\quit 1\n"
        "\\endif\n"
        f"SELECT COUNT(*) = {EXPECTED_RECORDS} AS exact_count FROM public.orders \\gset\n"
        "\\if :exact_count\n"
        "COMMIT;\n"
        "\\else\n"
        "ROLLBACK;\n"
        "\\quit 1\n"
        "\\endif\n"
    )


def validate_snapshot(stream):
    stream.seek(0)
    count = 0
    for count, document in enumerate(JsonArrayStream(stream), start=1):
        validate_document(document, count)
        timestamp = document["timestamp"]
        if not timestamp.endswith("Z"):
            raise DatasetError(f"record {count}.timestamp must use the generated UTC Z form")
        try:
            parsed = datetime.fromisoformat(timestamp[:-1] + "+00:00")
        except ValueError as exc:
            raise DatasetError(f"record {count}.timestamp must be an ISO-8601 UTC string") from exc
        if parsed.utcoffset() != timedelta(0):
            raise DatasetError(f"record {count}.timestamp must be UTC")
    if count != EXPECTED_RECORDS:
        raise DatasetError(f"dataset must contain exactly {EXPECTED_RECORDS} records; found {count}")
    return count


def write_copy_rows(stream, stdin):
    writer = csv.writer(stdin, lineterminator="\n")
    stream.seek(0)
    for document in JsonArrayStream(stream):
        writer.writerow([
            document["order_id"],
            json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
        ])
    stdin.write(copy_script_suffix())
    stdin.flush()


def import_snapshot(stream, container_id):
    with tempfile.TemporaryFile(mode="w+b") as error_output:
        process = subprocess.Popen(
            psql_command(container_id), stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=error_output, text=True, encoding="utf-8",
        )
        try:
            process.stdin.write(copy_script())
            write_copy_rows(stream, process.stdin)
        except Exception:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            process.wait()
            raise
        finally:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        return_code = process.wait()
        if return_code != 0:
            error_output.seek(0)
            details = error_output.read().decode("utf-8", errors="replace")[-1000:]
            raise LoaderError(
                "PostgreSQL import failed; the transaction was rolled back. "
                f"psql exit code: {return_code}. {details.strip()}"
            )


def with_snapshot_text(snapshot, operation):
    """Run a text-stream operation without closing its reusable binary snapshot."""
    snapshot.seek(0)
    text = io.TextIOWrapper(snapshot, encoding="utf-8", newline="")
    try:
        return operation(text)
    finally:
        text.detach()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, choices=tuple(SERVICES))
    parser.add_argument("--input", type=Path, default=DEFAULT_DATASET,
                        help="existing generator JSON array (never generated or modified)")
    args = parser.parse_args(argv)
    if not args.input.is_file():
        parser.error(f"dataset file does not exist: {args.input}")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        with tempfile.TemporaryFile(mode="w+b") as snapshot:
            with args.input.open("rb") as source:
                while chunk := source.read(64 * 1024):
                    snapshot.write(chunk)
            count = with_snapshot_text(snapshot, validate_snapshot)
            container_id = resolve_container(args.profile)
            ensure_target_is_empty(container_id)
            with_snapshot_text(
                snapshot,
                lambda text: import_snapshot(text, container_id),
            )
    except (DatasetError, LoaderError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f"PostgreSQL import failed: {exc}", file=sys.stderr)
        return 1
    print(f"Imported and verified {count} records into public.orders on {args.profile}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
