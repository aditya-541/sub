#!/bin/sh
set -eu

host="${FACTORY_BIND_HOST:-0.0.0.0}"
port="${FACTORY_PORT:-8000}"
exec uvicorn subnet_factory.api:app --host "$host" --port "$port"
