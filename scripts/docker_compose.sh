#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

COMPOSE="${COMPOSE:-docker compose}"
DEV_FILES=(-f docker-compose.yml -f docker-compose.dev.yml)

usage() {
  cat <<'EOF'
Usage: scripts/docker_compose.sh <command>

Commands:
  init      Create service-specific env files from examples if missing.
  build     Build the shared application image.
  up        Run agent-migrate, then start system-app, agent-app, agent-worker.
  dev       Start with source bind mounts and uvicorn --reload.
  down      Stop and remove containers.
  restart   Restart all services.
  verify    Run authenticated real-generation readiness inside agent-app.
  status    Show compose service status.
  logs      Follow logs.
  config    Validate the compose config without printing env values.

The system UI is exposed on http://<EC2_PUBLIC_IP>:8000 by default.
Only system-app is published; agent-app stays inside the Docker network.
EOF
}

ensure_env() {
  ensure_env_pair ".env.agent_app" ".env.agent_app.example"
  ensure_secret_env_pair \
    ".env.agent_app.secret" \
    ".env.agent_app.secret.example"
  ensure_env_pair ".env.system_app" ".env.system_app.example"
  assert_common_env_has_no_bedrock_bearer ".env.agent_app"
  assert_common_env_has_no_bedrock_bearer ".env.system_app"
}

assert_common_env_has_no_bedrock_bearer() {
  local file="$1"
  if [[ -f "$file" ]] \
    && grep -Eq '^[[:space:]]*AWS_BEARER_TOKEN_BEDROCK[[:space:]]*=' "$file"; then
    echo "AWS_BEARER_TOKEN_BEDROCK must be kept in .env.agent_app.secret, not $file." >&2
    exit 1
  fi
}

ensure_env_pair() {
  local target="$1"
  local example="$2"
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
  local target="$1"
  local example="$2"
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

ensure_data_dir() {
  mkdir -p data
}

verify_agent_generation() {
  local -a compose_files=("$@")
  $COMPOSE "${compose_files[@]}" exec -T agent-app python - <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.request

token = os.environ.get("AGENT_SYNC_API_TOKEN", "").strip()
os.environ.pop("AWS_BEARER_TOKEN_BEDROCK", None)
if not token:
    print(
        "agent generation readiness: AGENT_SYNC_API_TOKEN is unavailable",
        file=sys.stderr,
    )
    raise SystemExit(1)

request = urllib.request.Request(
    "http://127.0.0.1:8001/health/generation/ready",
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
}

cmd="${1:-up}"
case "$cmd" in
  init)
    ensure_env
    ensure_data_dir
    ;;
  build)
    ensure_env
    ensure_data_dir
    $COMPOSE build
    ;;
  up)
    ensure_env
    ensure_data_dir
    $COMPOSE up -d --build
    verify_agent_generation
    $COMPOSE ps
    ;;
  dev)
    ensure_env
    ensure_data_dir
    $COMPOSE "${DEV_FILES[@]}" up -d --build
    verify_agent_generation "${DEV_FILES[@]}"
    $COMPOSE "${DEV_FILES[@]}" ps
    ;;
  down)
    $COMPOSE down
    ;;
  restart)
    ensure_env
    ensure_data_dir
    $COMPOSE down
    $COMPOSE up -d --build
    verify_agent_generation
    $COMPOSE ps
    ;;
  verify)
    ensure_env
    verify_agent_generation
    ;;
  status)
    $COMPOSE ps
    ;;
  logs)
    $COMPOSE logs -f --tail "${TAIL_LINES:-120}"
    ;;
  config)
    ensure_env
    ensure_data_dir
    $COMPOSE config --quiet
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
