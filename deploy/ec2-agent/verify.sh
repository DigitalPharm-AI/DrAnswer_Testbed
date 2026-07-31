#!/usr/bin/env bash
set -Eeuo pipefail

base_dir="/opt/dranswer-agent"
current_dir="$base_dir/current"
base_url="${AGENT_VERIFY_BASE_URL:-http://127.0.0.1:8701}"
common_env_file="/etc/dranswer-agent/agent.env"
bedrock_env_file="/etc/dranswer-agent/bedrock.env"

if [[ ! -L "$current_dir" ]]; then
  echo "Current Agent release symlink is missing." >&2
  exit 1
fi
active_release_dir="$(readlink -f "$current_dir")"
case "$active_release_dir" in
  "$base_dir"/releases/*) ;;
  *)
    echo "Current Agent release resolves outside the release directory." >&2
    exit 1
    ;;
esac
python="$active_release_dir/venv/bin/python"
deploy_dir="$active_release_dir/deploy/ec2-agent"
release_env_file="$active_release_dir/release.env"
manifest_file="$active_release_dir/release-manifest.json"
if [[ ! -x "$python" || ! -f "$release_env_file" || ! -f "$manifest_file" ]]; then
  echo "Current Agent release is incomplete." >&2
  exit 1
fi
verified_metadata="$(
  "$python" \
    "$deploy_dir/verify_release_artifact.py" \
    --installed-dir "$active_release_dir"
)"
read -r verified_release_id verified_commit_sha <<<"$verified_metadata"

"$python" - \
  "$manifest_file" \
  "$release_env_file" \
  "$verified_release_id" \
  "$verified_commit_sha" <<'PY'
import json
import pathlib
import re
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
release_values = {}
for raw_line in pathlib.Path(sys.argv[2]).read_text(encoding="utf-8").splitlines():
    if "=" not in raw_line:
        continue
    key, value = raw_line.split("=", 1)
    release_values[key.strip()] = value.strip()
release_id = str(manifest.get("release_id") or "")
commit_sha = str(manifest.get("commit_sha") or "")
if release_id != sys.argv[3] or commit_sha != sys.argv[4]:
    raise SystemExit("installed release verification metadata mismatch")
if release_values.get("APP_RELEASE_VERSION") != release_id:
    raise SystemExit("APP_RELEASE_VERSION does not match the release manifest")
if release_values.get("AGENT_RELEASE_COMMIT_SHA") != commit_sha:
    raise SystemExit("AGENT_RELEASE_COMMIT_SHA does not match the release manifest")
if not re.fullmatch(r"[0-9a-f]{40,64}", commit_sha):
    raise SystemExit("release manifest commit SHA is invalid")
print(f"Active release: {release_id} ({commit_sha[:12]})")
PY

sudo -n "$python" \
  "$deploy_dir/validate_env.py" \
  "$common_env_file" \
  "$bedrock_env_file"

langfuse_enabled=false
if grep -Eiq '^[[:space:]]*LANGFUSE_EXPORT_ENABLED[[:space:]]*=[[:space:]]*(true|1|yes)[[:space:]]*$' "$common_env_file"; then
  langfuse_enabled=true
fi

echo "== enabled Agent units =="
for service in dranswer-agent-api.service dranswer-agent-worker.service; do
  if ! systemctl is-enabled --quiet "$service"; then
    echo "$service is not enabled for reboot recovery." >&2
    exit 1
  fi
done
if [[ "$langfuse_enabled" == "true" ]]; then
  if ! systemctl is-enabled --quiet dranswer-agent-langfuse-exporter.service; then
    echo "Langfuse exporter is configured but not enabled." >&2
    exit 1
  fi
elif systemctl is-enabled --quiet dranswer-agent-langfuse-exporter.service; then
  echo "Langfuse exporter is disabled in config but enabled in systemd." >&2
  exit 1
fi
if systemctl is-enabled --quiet dranswer-agent-migrate.service; then
  echo "Migration unit must remain static and dependency-driven." >&2
  exit 1
fi

echo "== active Agent services =="
services=(
  dranswer-agent-migrate.service
  dranswer-agent-api.service
  dranswer-agent-worker.service
)
if [[ "$langfuse_enabled" == "true" ]]; then
  services+=(dranswer-agent-langfuse-exporter.service)
fi
for service in "${services[@]}"; do
  if ! systemctl is-active --quiet "$service"; then
    echo "$service is not active." >&2
    exit 1
  fi
done
systemctl --no-pager --full status "${services[@]}" | sed -n '1,120p'

echo "== liveness =="
curl -fsS "$base_url/health"
echo

echo "== readiness =="
readiness_file="$(mktemp)"
trap 'rm -f "$readiness_file"' EXIT
status="$(
  curl -sS -o "$readiness_file" -w '%{http_code}' \
    "$base_url/health/ready"
)"
cat "$readiness_file"
echo
if [[ "$status" != "200" ]]; then
  echo "Readiness failed with HTTP $status" >&2
  exit 1
fi

echo "== authenticated generation readiness =="
"$python" \
  "$deploy_dir/probe_generation.py" \
  "$common_env_file" \
  --base-url "$base_url" \
  --timeout-seconds "${AGENT_GENERATION_VERIFY_TIMEOUT_SECONDS:-120}"

echo "== authenticated Agent ops readiness =="
ops_args=()
"$python" \
  "$deploy_dir/probe_ops_readiness.py" \
  "$common_env_file" \
  --base-url "$base_url" \
  --timeout-seconds "${AGENT_OPS_VERIFY_TIMEOUT_SECONDS:-60}" \
  "${ops_args[@]}"

echo "Agent verification: ok"
