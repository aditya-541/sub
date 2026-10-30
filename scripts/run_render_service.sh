#!/usr/bin/env bash
set -Eeuo pipefail

factory-worker &
worker_pid=$!

shutdown() {
  kill "$worker_pid" 2>/dev/null || true
  wait "$worker_pid" 2>/dev/null || true
}
trap shutdown EXIT INT TERM

uvicorn subnet_factory.api:app --host "${FACTORY_BIND_HOST:-0.0.0.0}" --port "${PORT:-${FACTORY_PORT:-10000}}"
