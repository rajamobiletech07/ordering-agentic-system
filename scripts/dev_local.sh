#!/usr/bin/env bash
# Run every service as a local process (no app containers) against the compose Postgres/Redis.
# Usage: scripts/dev_local.sh up|down|status      Logs: .run/<service>.log
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-.venv/bin/python}
RUN=.run
mkdir -p "$RUN"
export OTEL_ENABLED=${OTEL_ENABLED:-false}

SERVICES=(
  "mcp-recommendation|$PY -m ordering.mcp_servers.recommendation.server|9001"
  "mcp-ordering|$PY -m ordering.mcp_servers.ordering.server|9002"
  "mcp-payment|$PY -m ordering.mcp_servers.payment.server|9003"
  "mcp-notification|$PY -m ordering.mcp_servers.notification.server|9004"
  "notification-worker|$PY -m ordering.notification_worker.worker|"
  "llm-gateway|$PY -m uvicorn ordering.llm_gateway.main:app --port 8020|8020"
  "agent-service|$PY -m uvicorn ordering.agents.service:app --port 8010|8010"
  "api-gateway|$PY -m uvicorn ordering.api_gateway.main:app --port 8000|8000"
)

up() {
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name cmd port <<<"$entry"
    if [[ -f "$RUN/$name.pid" ]] && kill -0 "$(cat "$RUN/$name.pid")" 2>/dev/null; then echo "$name already running"; continue; fi
    SERVICE_NAME=$name nohup $cmd >"$RUN/$name.log" 2>&1 &
    echo $! >"$RUN/$name.pid"
    echo "started $name (pid $!)"
  done
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name _ port <<<"$entry"
    [[ -z "$port" ]] && continue
    for _ in $(seq 1 60); do curl -sf "localhost:$port/healthz" >/dev/null && break; sleep 0.5; done
    curl -sf "localhost:$port/healthz" >/dev/null && echo "✓ $name :$port" || echo "✗ $name :$port (see $RUN/$name.log)"
  done
}

down() {
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name _ _ <<<"$entry"
    if [[ -f "$RUN/$name.pid" ]]; then kill "$(cat "$RUN/$name.pid")" 2>/dev/null || true; rm -f "$RUN/$name.pid"; echo "stopped $name"; fi
  done
}

status() {
  for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name _ port <<<"$entry"
    if [[ -f "$RUN/$name.pid" ]] && kill -0 "$(cat "$RUN/$name.pid")" 2>/dev/null; then echo "running $name"; else echo "stopped $name"; fi
  done
}

"${1:-up}"
