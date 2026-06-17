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
  init      Create .env and app-specific env files from examples if missing.
  build     Build the shared application image.
  up        Start phr-app, agent-app, agent-worker, system-app.
  dev       Start with source bind mounts and uvicorn --reload.
  down      Stop and remove containers.
  restart   Restart all services.
  status    Show compose service status.
  logs      Follow logs.
  config    Validate the compose config without printing env values.

The system UI is exposed on http://<EC2_PUBLIC_IP>:8000 by default.
Only system-app is published; agent-app and phr-app stay inside the Docker network.
EOF
}

ensure_env() {
  ensure_env_pair ".env" ".env.example"
  ensure_env_pair ".env.agent_app" ".env.agent_app.example"
  ensure_env_pair ".env.system_app" ".env.system_app.example"
  ensure_env_pair ".env.phr_app" ".env.phr_app.example"
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

ensure_data_dir() {
  mkdir -p data
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
    $COMPOSE ps
    ;;
  dev)
    ensure_env
    ensure_data_dir
    $COMPOSE "${DEV_FILES[@]}" up -d --build
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
    $COMPOSE ps
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
