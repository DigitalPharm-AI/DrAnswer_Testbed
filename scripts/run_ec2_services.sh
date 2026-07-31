#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENV_DIR="${VENV_DIR:-$ROOT_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUN_DIR="${RUN_DIR:-$ROOT_DIR/.run}"
LOG_DIR="${LOG_DIR:-$ROOT_DIR/data/logs}"

SYSTEM_HOST="${SYSTEM_HOST:-0.0.0.0}"
INTERNAL_HOST="${INTERNAL_HOST:-127.0.0.1}"
SYSTEM_PORT="${SYSTEM_PORT:-8000}"
AGENT_PORT="${AGENT_PORT:-8001}"
RELOAD="${RELOAD:-0}"

PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"
UVICORN="$VENV_DIR/bin/uvicorn"

usage() {
  cat <<'EOF'
Usage: scripts/run_ec2_services.sh <command>

Commands:
  install   Create .venv and install requirements.
  start     Start system_app, migrate Agent DB, then start agent services.
  start-agent
            Start only agent_app and agent_worker.
  stop      Stop all services.
  stop-agent
            Stop only agent_app and agent_worker.
  restart   Stop then start all three services.
  restart-agent
            Restart only agent_app and agent_worker.
  verify-generation
            Run authenticated real-generation readiness against agent_app.
  status    Print process status and health checks.
  status-agent
            Print only agent_app/agent_worker process status and health check.
  logs      Tail service logs.
  logs-agent
            Tail only agent_app/agent_worker logs.

Useful env vars:
  PYTHON_BIN=python3.11
  SYSTEM_HOST=0.0.0.0
  INTERNAL_HOST=127.0.0.1
  SYSTEM_PORT=8000 AGENT_PORT=8001
  RELOAD=1
EOF
}

ensure_dirs() {
  mkdir -p "$RUN_DIR" "$LOG_DIR" "$ROOT_DIR/data"
}

ensure_env_files() {
  ensure_env_pair ".env.agent_app" ".env.agent_app.example"
  ensure_secret_env_pair \
    ".env.agent_app.secret" \
    ".env.agent_app.secret.example"
  ensure_env_pair ".env.system_app" ".env.system_app.example"
}

ensure_env_pair() {
  local target="$ROOT_DIR/$1"
  local example="$ROOT_DIR/$2"
  if [[ -f "$target" ]]; then
    return
  fi
  if [[ ! -f "$example" ]]; then
    echo "Missing $target and $example." >&2
    exit 1
  fi
  cp "$example" "$target"
  echo "Created $target from $example." >&2
}

ensure_secret_env_pair() {
  local target="$ROOT_DIR/$1"
  local example="$ROOT_DIR/$2"
  if [[ ! -f "$target" ]]; then
    if [[ ! -f "$example" ]]; then
      echo "Missing $target and $example." >&2
      exit 1
    fi
    (
      umask 077
      cp "$example" "$target"
    )
    echo "Created $target from $example with mode 600." >&2
  fi
  chmod 600 "$target"
}

source_env_file() {
  local file="$1"
  if [[ -f "$file" ]]; then
    # shellcheck disable=SC1090
    source "$file"
  fi
}

export_service_env_defaults() {
  export AGENT_BASE_URL="${AGENT_BASE_URL:-http://127.0.0.1:${AGENT_PORT}}"
  export SYSTEM_BASE_URL="${SYSTEM_BASE_URL:-http://127.0.0.1:${SYSTEM_PORT}}"
}

assert_common_env_has_no_bedrock_bearer() {
  local file="$1"
  if [[ -f "$file" ]] \
    && grep -Eq '^[[:space:]]*AWS_BEARER_TOKEN_BEDROCK[[:space:]]*=' "$file"; then
    echo "AWS_BEARER_TOKEN_BEDROCK must be kept in the Agent-only credential overlay." >&2
    exit 1
  fi
}

load_service_env() {
  local service_env="$1"
  local include_agent_secret="${2:-false}"
  local common_env="$ROOT_DIR/.env.${service_env}"
  assert_common_env_has_no_bedrock_bearer "$common_env"
  set -a
  unset AWS_BEARER_TOKEN_BEDROCK
  source_env_file "$common_env"
  if [[ "$service_env" == "agent_app" && "$include_agent_secret" == "true" ]]; then
    source_env_file "$ROOT_DIR/.env.agent_app.secret"
  fi
  set +a
  export DA_DRUG_SERVICE="$service_env"
  export_service_env_defaults
}

install_deps() {
  ensure_dirs
  if [[ ! -x "$PYTHON" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
  "$PYTHON" -m pip install --upgrade pip
  "$PIP" install -r "$ROOT_DIR/requirements.txt"
}

pid_file() {
  echo "$RUN_DIR/$1.pid"
}

is_running() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1
}

service_pid() {
  local file
  file="$(pid_file "$1")"
  if [[ -f "$file" ]]; then
    cat "$file"
  fi
}

start_service() {
  local name="$1"
  local app="$2"
  local host="$3"
  local port="$4"
  local service_env="$5"
  local pidfile
  local pid
  pidfile="$(pid_file "$name")"
  pid="$(service_pid "$name" || true)"
  if is_running "$pid"; then
    echo "$name already running (pid=$pid)"
    return
  fi

  local reload_args=()
  if [[ "$RELOAD" == "1" ]]; then
    reload_args=(--reload)
  fi

  echo "Starting $name on $host:$port"
  (
    if [[ "$service_env" == "agent_app" ]]; then
      load_service_env "$service_env" "true"
      export AGENT_EMBEDDED_WORKER_ENABLED=false
    else
      load_service_env "$service_env"
    fi
    nohup "$UVICORN" "$app" --host "$host" --port "$port" "${reload_args[@]}" >"$LOG_DIR/$name.log" 2>&1 &
    echo $! >"$pidfile"
  )
  sleep 1

  pid="$(service_pid "$name" || true)"
  if ! is_running "$pid"; then
    echo "Failed to start $name. Last log lines:" >&2
    tail -n 80 "$LOG_DIR/$name.log" >&2 || true
    exit 1
  fi
}

start_worker_service() {
  local name="agent_worker"
  local pidfile
  local pid
  pidfile="$(pid_file "$name")"
  pid="$(service_pid "$name" || true)"
  if is_running "$pid"; then
    echo "$name already running (pid=$pid)"
    return
  fi

  echo "Starting $name"
  (
    load_service_env "agent_app" "true"
    export AGENT_EMBEDDED_WORKER_ENABLED=false
    nohup "$PYTHON" -m agent_app.worker_main >"$LOG_DIR/$name.log" 2>&1 &
    echo $! >"$pidfile"
  )
  sleep 1

  pid="$(service_pid "$name" || true)"
  if ! is_running "$pid"; then
    echo "Failed to start $name. Last log lines:" >&2
    tail -n 80 "$LOG_DIR/$name.log" >&2 || true
    exit 1
  fi
}

run_agent_migrations() {
  echo "Migrating Agent database schema"
  (
    load_service_env "agent_app"
    "$PYTHON" -m agent_app.migrate
  )
}

healthcheck() {
  local name="$1"
  local url="$2"
  if ! command -v curl >/dev/null 2>&1; then
    echo "$name health: skipped (curl not found)"
    return
  fi
  for _ in $(seq 1 30); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      echo "$name health: ok"
      return
    fi
    sleep 1
  done
  echo "$name health: failed ($url)" >&2
  tail -n 80 "$LOG_DIR/$name.log" >&2 || true
  exit 1
}

generation_healthcheck() {
  (
    load_service_env "agent_app"
    "$PYTHON" - "$AGENT_PORT" <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.request

token = os.environ.get("AGENT_SYNC_API_TOKEN", "").strip()
if not token:
    print(
        "agent generation readiness: AGENT_SYNC_API_TOKEN is unavailable",
        file=sys.stderr,
    )
    raise SystemExit(1)

request = urllib.request.Request(
    f"http://127.0.0.1:{int(sys.argv[1])}/health/generation/ready",
    headers={"Authorization": f"Bearer {token}"},
)
deadline = time.monotonic() + 60
while True:
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            status_code = int(response.status)
            payload = json.loads(response.read().decode("utf-8"))
        break
    except urllib.error.HTTPError as exc:
        print(
            f"agent generation readiness: HTTP {int(exc.code)}",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except json.JSONDecodeError:
        print(
            "agent generation readiness: malformed response",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    except (OSError, TimeoutError, urllib.error.URLError):
        if time.monotonic() >= deadline:
            print(
                "agent generation readiness: endpoint unavailable",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        time.sleep(1)

if (
    status_code != 200
    or not isinstance(payload, dict)
    or payload.get("status") != "ready"
):
    print("agent generation readiness: not ready", file=sys.stderr)
    raise SystemExit(1)
print("agent generation readiness: ready")
PY
  )
}

start_all() {
  ensure_dirs
  ensure_env_files
  if [[ ! -x "$UVICORN" ]]; then
    install_deps
  fi
  start_service "system_app" "system_app.main:app" "$SYSTEM_HOST" "$SYSTEM_PORT" "system_app"
  healthcheck "system_app" "http://127.0.0.1:$SYSTEM_PORT/health"
  run_agent_migrations
  start_service "agent_app" "agent_app.main:app" "$INTERNAL_HOST" "$AGENT_PORT" "agent_app"
  healthcheck "agent_app" "http://127.0.0.1:$AGENT_PORT/health"
  start_worker_service
  generation_healthcheck
  echo
  echo "System UI: http://<EC2_PUBLIC_IP>:$SYSTEM_PORT"
  echo "Logs: $LOG_DIR"
}

start_agent() {
  ensure_dirs
  ensure_env_files
  if [[ ! -x "$UVICORN" ]]; then
    install_deps
  fi
  run_agent_migrations
  start_service "agent_app" "agent_app.main:app" "$INTERNAL_HOST" "$AGENT_PORT" "agent_app"
  healthcheck "agent_app" "http://127.0.0.1:$AGENT_PORT/health"
  start_worker_service
  generation_healthcheck
}

stop_service() {
  local name="$1"
  local pidfile
  local pid
  pidfile="$(pid_file "$name")"
  pid="$(service_pid "$name" || true)"
  if ! is_running "$pid"; then
    echo "$name not running"
    rm -f "$pidfile"
    return
  fi
  echo "Stopping $name (pid=$pid)"
  kill "$pid" >/dev/null 2>&1 || true
  for _ in $(seq 1 20); do
    if ! is_running "$pid"; then
      rm -f "$pidfile"
      return
    fi
    sleep 0.5
  done
  echo "Force stopping $name"
  kill -9 "$pid" >/dev/null 2>&1 || true
  rm -f "$pidfile"
}

stop_all() {
  stop_service "agent_worker"
  stop_service "agent_app"
  stop_service "system_app"
}

status_service() {
  local name="$1"
  local port="$2"
  local pid
  pid="$(service_pid "$name" || true)"
  if is_running "$pid"; then
    printf "%-12s running  pid=%-8s  port=%s\n" "$name" "$pid" "$port"
  else
    printf "%-12s stopped  port=%s\n" "$name" "$port"
  fi
}

status_all() {
  status_service "system_app" "$SYSTEM_PORT"
  status_service "agent_app" "$AGENT_PORT"
  status_service "agent_worker" "-"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "http://127.0.0.1:$SYSTEM_PORT/health" >/dev/null 2>&1 && echo "system_app health: ok" || echo "system_app health: unavailable"
    curl -fsS "http://127.0.0.1:$AGENT_PORT/health" >/dev/null 2>&1 && echo "agent_app health: ok" || echo "agent_app health: unavailable"
  fi
}

status_agent() {
  status_service "agent_app" "$AGENT_PORT"
  status_service "agent_worker" "-"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "http://127.0.0.1:$AGENT_PORT/health" >/dev/null 2>&1 && echo "agent_app health: ok" || echo "agent_app health: unavailable"
  fi
}

tail_logs() {
  ensure_dirs
  touch "$LOG_DIR/system_app.log" "$LOG_DIR/agent_app.log" "$LOG_DIR/agent_worker.log"
  tail -n "${TAIL_LINES:-80}" -f "$LOG_DIR/system_app.log" "$LOG_DIR/agent_app.log" "$LOG_DIR/agent_worker.log"
}

tail_agent_logs() {
  ensure_dirs
  touch "$LOG_DIR/agent_app.log" "$LOG_DIR/agent_worker.log"
  tail -n "${TAIL_LINES:-120}" -f "$LOG_DIR/agent_app.log" "$LOG_DIR/agent_worker.log"
}

command="${1:-start}"
case "$command" in
  install)
    install_deps
    ;;
  start)
    start_all
    ;;
  start-agent)
    start_agent
    ;;
  stop)
    stop_all
    ;;
  stop-agent)
    stop_service "agent_worker"
    stop_service "agent_app"
    ;;
  restart)
    stop_all
    start_all
    ;;
  restart-agent)
    stop_service "agent_worker"
    stop_service "agent_app"
    start_agent
    ;;
  verify-generation)
    ensure_env_files
    generation_healthcheck
    ;;
  status)
    status_all
    ;;
  status-agent)
    status_agent
    ;;
  logs)
    tail_logs
    ;;
  logs-agent)
    tail_agent_logs
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
