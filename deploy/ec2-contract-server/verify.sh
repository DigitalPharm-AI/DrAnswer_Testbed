#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
  exec sudo "$0" "$@"
fi

expected_release="${1:-}"
expected_worker_state="${2:-stopped}"
if [[ -z "$expected_release" ]]; then
  echo "Usage: $0 <expected-release> <stopped|running>" >&2
  exit 2
fi
if [[ "$expected_worker_state" != "stopped" && "$expected_worker_state" != "running" ]]; then
  echo "Worker state must be stopped or running." >&2
  exit 2
fi

env_file="/etc/dranswer-agent-contract/contract.env"
release_env_file="/etc/dranswer-agent-contract/release.env"
set -a
source "$env_file"
source "$release_env_file"
set +a

if [[ "$APP_RELEASE_VERSION" != "$expected_release" ]]; then
  echo "release.env does not match the candidate release." >&2
  exit 1
fi
if ! systemctl is-active --quiet dranswer-agent-contract-api.service; then
  echo "Contract API service is not active." >&2
  exit 1
fi

port="${CONTRACT_PORT:-8701}"
base_url="http://127.0.0.1:${port}"
health_json=""
for _attempt in $(seq 1 30); do
  if health_json="$(
    curl \
      --fail \
      --silent \
      --show-error \
      --max-time 2 \
      "${base_url}/health"
  )"
  then
    break
  fi
  sleep 1
done
ready_json="$(
  curl \
    --fail \
    --silent \
    --show-error \
    --max-time 3 \
    "${base_url}/health/ready"
)"

/usr/bin/env \
  HEALTH_JSON="$health_json" \
  READY_JSON="$ready_json" \
  EXPECTED_RELEASE="$expected_release" \
  EXPECTED_CALLBACK_MODE="$CALLBACK_MODE" \
  /opt/dranswer-agent-contract/current/.venv/bin/python -c "
import json
import os
health = json.loads(os.environ['HEALTH_JSON'])
ready = json.loads(os.environ['READY_JSON'])
assert health['status'] == 'ok'
assert health['release'] == os.environ['EXPECTED_RELEASE']
assert ready['status'] == 'ready'
assert ready['release'] == os.environ['EXPECTED_RELEASE']
assert ready['callback_mode'] == os.environ['EXPECTED_CALLBACK_MODE']
assert ready['database_ready'] is True
expected_delivery = os.environ['EXPECTED_CALLBACK_MODE'] == 'deliver'
assert ready['callback_delivery_ready'] is expected_delivery
"

if [[ "$expected_worker_state" == "running" ]]; then
  systemctl is-active --quiet dranswer-agent-contract-callback.service
else
  if systemctl is-active --quiet dranswer-agent-contract-callback.service; then
    echo "Callback worker is active before it is allowed." >&2
    exit 1
  fi
fi

echo "Contract test server release $expected_release is verified."
