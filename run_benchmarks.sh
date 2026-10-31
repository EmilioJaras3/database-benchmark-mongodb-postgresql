#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
  printf 'Usage: %s <csv-output-path>\n' "$0" >&2
  exit 2
fi

CALLER_DIR=$PWD
CSV_PATH=$1
if [[ "$CSV_PATH" != /* ]]; then
  CSV_PATH="$CALLER_DIR/$CSV_PATH"
fi

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

profiles=(PROFILE_A_BASELINE PROFILE_B_MEDIUM PROFILE_C_SEVERE)
engines=(mongodb postgresql)
queries=(Q1 Q2)
concurrencies=(1 10 50)
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

  printf 'Profile %s is running. Manually provision both databases and verify readiness.\n' "$profile"
  read -r -p 'Type READY after provisioning and readiness checks: ' confirmation
  if [[ "$confirmation" != READY ]]; then
    printf 'Readiness was not confirmed; stopping the benchmark services.\n' >&2
    exit 1
  fi

  for engine in "${engines[@]}"; do
    for query in "${queries[@]}"; do
      for concurrency in "${concurrencies[@]}"; do
        python3 benchmark_runner.py \
          --csv "$CSV_PATH" \
          --engine "$engine" \
          --profile "$profile" \
          --query "$query" \
          --concurrency "$concurrency"
      done
    done
  done

  docker compose stop "$mongo_service" "$postgres_service" >/dev/null
  ACTIVE_MONGO=
  ACTIVE_POSTGRES=
done
