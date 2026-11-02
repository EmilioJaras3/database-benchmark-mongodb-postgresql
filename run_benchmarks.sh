#!/usr/bin/env bash
set -Eeuo pipefail

RESUME_MODE=false
if [[ ${1:-} == "--resume" ]]; then
  RESUME_MODE=true
  shift
fi

if [[ $# -ne 1 ]]; then
  printf 'Usage: %s [--resume] <csv-output-path>\n' "$0" >&2
  exit 2
fi

CALLER_DIR=$PWD
CSV_PATH=$1
if [[ "$CSV_PATH" != /* ]]; then
  CSV_PATH="$CALLER_DIR/$CSV_PATH"
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

if [[ "$RESUME_MODE" == true ]]; then
  if [[ ! -f "$CSV_PATH" ]]; then
    printf 'Cannot resume: partial CSV does not exist.\n' >&2
    exit 1
  fi
  python3 - "$CSV_PATH" <<'PY'
import sys
from pathlib import Path

from check_spec import validate_partial_csv

errors, row_count, _ = validate_partial_csv(Path(sys.argv[1]))
if errors:
    print("Cannot resume: partial CSV validation failed: " + "; ".join(errors), file=sys.stderr)
    raise SystemExit(1)
print(f"Resume preflight passed: preserving {row_count} valid existing rows.")
PY
fi

profiles=(PROFILE_A_BASELINE PROFILE_B_MEDIUM PROFILE_C_SEVERE)
engines=(mongodb postgresql)
queries=(Q1 Q2)
concurrencies=(1 10 50)
PROMETHEUS_URL=${PROMETHEUS_URL:-http://127.0.0.1:9091}
READINESS_TIMEOUT_SECONDS=60
benchmark_services=(
  mongo-profile-a mongo-profile-b mongo-profile-c
  postgres-profile-a postgres-profile-b postgres-profile-c
  cadvisor node-exporter prometheus
)

running_services=$(docker compose ps --status running --services)
for service in "${benchmark_services[@]}"; do
  if grep -Fxq "$service" <<<"$running_services"; then
    printf 'Refusing to alter already-running service: %s\n' "$service" >&2
    exit 1
  fi
done

ACTIVE_MONGO=
ACTIVE_POSTGRES=
MONITORING_STARTED=0
runner_resume_args=()
if [[ "$RESUME_MODE" == true ]]; then
  runner_resume_args+=(--resume)
fi
cleanup() {
  local exit_status=$?
  local cleanup_failed=0
  if [[ -n "$ACTIVE_MONGO" && -n "$ACTIVE_POSTGRES" ]]; then
    if ! docker compose stop "$ACTIVE_MONGO" "$ACTIVE_POSTGRES" >/dev/null; then
      printf 'Failed to stop the active database services; stop them manually.\n' >&2
      cleanup_failed=1
    fi
  fi
  if [[ "$MONITORING_STARTED" -eq 1 ]]; then
    if ! docker compose stop cadvisor node-exporter prometheus >/dev/null; then
      printf 'Failed to stop monitoring services; stop them manually.\n' >&2
      cleanup_failed=1
    fi
  fi
  if [[ "$cleanup_failed" -eq 1 && "$exit_status" -eq 0 ]]; then
    exit_status=1
  fi
  return "$exit_status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

readiness_failure() {
  printf 'Readiness check failed for profile %s: %s. No benchmark scenarios were started.\n' "$profile" "$1" >&2
  exit 1
}

check_mongo_ready() {
  local count attempt
  for ((attempt = 1; attempt <= READINESS_TIMEOUT_SECONDS; attempt++)); do
    if count=$(docker compose exec -T "$ACTIVE_MONGO" mongosh --quiet --eval \
      'if (db.adminCommand({ping: 1}).ok !== 1) quit(2); print(db.getSiblingDB("test").orders.countDocuments({}))' 2>/dev/null); then
      if [[ "$count" != 500000 ]]; then
        readiness_failure "$ACTIVE_MONGO test.orders has ${count:-no readable count}; expected 500000"
      fi
      return
    fi
    sleep 1
  done
  readiness_failure "$ACTIVE_MONGO did not answer its ping/count check within ${READINESS_TIMEOUT_SECONDS}s"
}

check_postgres_ready() {
  local count attempt
  for ((attempt = 1; attempt <= READINESS_TIMEOUT_SECONDS; attempt++)); do
    if count=$(docker compose exec -T "$ACTIVE_POSTGRES" sh -c \
      'PGPASSWORD="$POSTGRES_PASSWORD" psql -U postgres -d postgres -Atqc "SELECT COUNT(*) FROM public.orders"' 2>/dev/null); then
      if [[ "$count" != 500000 ]]; then
        readiness_failure "$ACTIVE_POSTGRES public.orders has ${count:-no readable count}; expected 500000"
      fi
      return
    fi
    sleep 1
  done
  readiness_failure "$ACTIVE_POSTGRES did not answer its public.orders count check within ${READINESS_TIMEOUT_SECONDS}s"
}

check_prometheus_target() {
  local job=$1 target=$2 response attempt
  for ((attempt = 1; attempt <= READINESS_TIMEOUT_SECONDS; attempt++)); do
    if response=$(curl --fail --silent --show-error --get \
      "$PROMETHEUS_URL/api/v1/query" --data-urlencode "query=up{job=\"$job\",instance=\"$target\"}" 2>/dev/null) \
      && printf '%s' "$response" | python3 -c \
        'import json,sys; d=json.load(sys.stdin); r=d.get("data",{}).get("result",[]); sys.exit(0 if d.get("status")=="success" and len(r)==1 and r[0].get("value",[None,None])[1]=="1" else 1)' \
        >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  readiness_failure "Prometheus target $job at $target did not become up within ${READINESS_TIMEOUT_SECONDS}s"
}

check_profile_readiness() {
  check_mongo_ready
  check_postgres_ready
  check_prometheus_target cadvisor cadvisor:8080
  check_prometheus_target node-exporter node-exporter:9100
  printf 'Readiness checks passed for %s: MongoDB and PostgreSQL each contain 500000 orders; cAdvisor and Node Exporter are up.\n' "$profile"
}

for profile in "${profiles[@]}"; do
  case "$profile" in
    PROFILE_A_BASELINE) suffix=a ;;
    PROFILE_B_MEDIUM) suffix=b ;;
    PROFILE_C_SEVERE) suffix=c ;;
  esac
  mongo_service="mongo-profile-$suffix"
  postgres_service="postgres-profile-$suffix"
  ACTIVE_MONGO=$mongo_service
  ACTIVE_POSTGRES=$postgres_service
  MONITORING_STARTED=1

  docker compose --profile "$profile" up -d \
    "$mongo_service" "$postgres_service" cadvisor node-exporter prometheus >/dev/null

  check_profile_readiness

  for engine in "${engines[@]}"; do
    for query in "${queries[@]}"; do
      for concurrency in "${concurrencies[@]}"; do
        python3 "$SCRIPT_DIR/benchmark_runner.py" \
          --csv "$CSV_PATH" \
          --engine "$engine" \
          --profile "$profile" \
          --query "$query" \
          --concurrency "$concurrency" \
          "${runner_resume_args[@]}"
      done
    done
  done

  docker compose stop "$mongo_service" "$postgres_service" >/dev/null
  ACTIVE_MONGO=
  ACTIVE_POSTGRES=
done
