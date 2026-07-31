#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <installed-release-id>" >&2
  exit 2
fi

release_id="$1"
if [[ ! "$release_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]]; then
  echo "Invalid Agent release ID." >&2
  exit 2
fi
if [[ -z "${AGENT_DB_BACKUP_ID:-}" \
   || "${AGENT_DB_BACKUP_ID}" == *CHANGE_ME* ]]; then
  echo "AGENT_DB_BACKUP_ID must identify a verified backup/PITR point." >&2
  echo "For a disposable synthetic test DB, record its approved disposable ID." >&2
  exit 1
fi
if [[ ! "${AGENT_DB_BACKUP_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$ ]]; then
  echo "AGENT_DB_BACKUP_ID contains unsupported characters." >&2
  exit 1
fi
if [[ "${AGENT_SCHEMA_FORWARD_COMPATIBLE:-false}" != "true" ]]; then
  echo "Set AGENT_SCHEMA_FORWARD_COMPATIBLE=true only after confirming" >&2
  echo "the previous app can run against every migration in this release." >&2
  exit 1
fi

base_dir="/opt/dranswer-agent"
release_dir="$base_dir/releases/$release_id"
current_link="$base_dir/current"
previous_link="$base_dir/previous"
lock_file="$base_dir/deploy.lock"
common_env_file="/etc/dranswer-agent/agent.env"
bedrock_env_file="/etc/dranswer-agent/bedrock.env"
service_dir="/etc/systemd/system"
canary_port="${AGENT_CANARY_PORT:-18701}"
if [[ ! "$canary_port" =~ ^[0-9]+$ ]] \
   || (( 10#$canary_port < 1024 \
      || 10#$canary_port > 65535 \
      || 10#$canary_port == 8701 )); then
  echo "AGENT_CANARY_PORT must be an unused port from 1024-65535 except 8701." >&2
  exit 1
fi
canary_unit="dranswer-agent-canary-${release_id//./-}"
unit_names=(
  dranswer-agent-api.service
  dranswer-agent-worker.service
  dranswer-agent-migrate.service
  dranswer-agent-langfuse-exporter.service
)
enable_units=(
  dranswer-agent-api.service
  dranswer-agent-worker.service
  dranswer-agent-langfuse-exporter.service
)
previous_release=""
unit_backup_dir=""
mutation_started=false
activation_succeeded=false

declare -A previous_enabled
declare -A previous_active

if [[ ! -f "$lock_file" ]]; then
  echo "Agent deployment lock is missing; run prepare_host/install first." >&2
  exit 1
fi
exec 9>"$lock_file"
if ! flock -n 9; then
  echo "Another Agent release install or activation is in progress." >&2
  exit 1
fi

atomic_link() {
  local target="$1"
  local link="$2"
  local candidate="${link}.new.$$"
  sudo -n rm -f -- "$candidate"
  sudo -n ln -s -- "$target" "$candidate"
  if ! sudo -n mv -Tf -- "$candidate" "$link"; then
    sudo -n rm -f -- "$candidate"
    return 1
  fi
}

langfuse_enabled() {
  grep -Eiq \
    '^[[:space:]]*LANGFUSE_EXPORT_ENABLED[[:space:]]*=[[:space:]]*(true|1|yes)[[:space:]]*$' \
    "$common_env_file"
}

stop_canary() {
  sudo -n systemctl stop "${canary_unit}.service" >/dev/null 2>&1 || true
  sudo -n systemctl reset-failed "${canary_unit}.service" >/dev/null 2>&1 || true
}

restore_unit_files() {
  local unit
  for unit in "${unit_names[@]}"; do
    if sudo -n test -f "$unit_backup_dir/$unit"; then
      sudo -n install -m 0644 -o root -g root \
        "$unit_backup_dir/$unit" "$service_dir/$unit" || return 1
    else
      sudo -n rm -f -- "$service_dir/$unit" || return 1
    fi
  done
  sudo -n systemctl daemon-reload || return 1
}

restore_enable_state() {
  local unit
  for unit in "${enable_units[@]}"; do
    if [[ "${previous_enabled[$unit]:-false}" == "true" ]]; then
      sudo -n systemctl enable "$unit" >/dev/null || return 1
    else
      sudo -n systemctl disable "$unit" >/dev/null 2>&1 || {
        if systemctl is-enabled --quiet "$unit"; then
          return 1
        fi
      }
    fi
  done
}

rollback_failed_activation() {
  local rollback_failed=0
  local unit
  set +e
  echo "Activation failed; restoring the previous Agent application release." >&2
  sudo -n systemctl stop dranswer-agent-worker.service \
    dranswer-agent-langfuse-exporter.service \
    dranswer-agent-api.service >/dev/null 2>&1
  for unit in dranswer-agent-worker.service \
    dranswer-agent-langfuse-exporter.service \
    dranswer-agent-api.service; do
    if systemctl is-active --quiet "$unit"; then
      rollback_failed=1
    fi
  done
  sudo -n systemctl reset-failed \
    dranswer-agent-migrate.service \
    dranswer-agent-api.service \
    dranswer-agent-worker.service \
    dranswer-agent-langfuse-exporter.service >/dev/null 2>&1
  restore_unit_files || rollback_failed=1
  if [[ -n "$previous_release" && -d "$previous_release" ]]; then
    atomic_link "$previous_release" "$current_link" || rollback_failed=1
  else
    sudo -n rm -f -- "$current_link" || rollback_failed=1
  fi
  restore_enable_state || rollback_failed=1
  if [[ -n "$previous_release" && -d "$previous_release" ]]; then
    if [[ "${previous_active[dranswer-agent-api.service]:-false}" == "true" ]]; then
      sudo -n systemctl restart dranswer-agent-migrate.service \
        || rollback_failed=1
      sudo -n systemctl start dranswer-agent-api.service \
        || rollback_failed=1
    fi
    if [[ "${previous_active[dranswer-agent-worker.service]:-false}" == "true" ]]; then
      sudo -n systemctl start dranswer-agent-worker.service \
        || rollback_failed=1
    fi
    if [[ "${previous_active[dranswer-agent-langfuse-exporter.service]:-false}" == "true" ]]; then
      sudo -n systemctl start dranswer-agent-langfuse-exporter.service \
        || rollback_failed=1
    fi
    if [[ "${previous_active[dranswer-agent-api.service]:-false}" == "true" ]]; then
      if curl -fsS "http://127.0.0.1:8701/health/ready" >/dev/null \
         && "$previous_release/venv/bin/python" \
              "$previous_release/deploy/ec2-agent/probe_generation.py" \
              "$common_env_file" \
              --base-url "http://127.0.0.1:8701" \
              --timeout-seconds \
                "${AGENT_GENERATION_VERIFY_TIMEOUT_SECONDS:-120}" \
              >/dev/null; then
        echo "Previous Agent release readiness reverified." >&2
      else
        echo "WARNING: previous Agent release was restored but did not reverify." >&2
        rollback_failed=1
      fi
    fi
  fi
  for unit in "${enable_units[@]}"; do
    if [[ "${previous_enabled[$unit]:-false}" == "true" ]]; then
      systemctl is-enabled --quiet "$unit" || rollback_failed=1
    elif systemctl is-enabled --quiet "$unit"; then
      rollback_failed=1
    fi
    if [[ "${previous_active[$unit]:-false}" == "true" ]]; then
      systemctl is-active --quiet "$unit" || rollback_failed=1
    elif systemctl is-active --quiet "$unit"; then
      rollback_failed=1
    fi
  done
  if (( rollback_failed == 0 )); then
    echo "Application rollback verified. Database migrations were not downgraded." >&2
    echo "Backup/PITR reference: ${AGENT_DB_BACKUP_ID}" >&2
  else
    echo "CRITICAL: automatic application rollback did not fully restore state." >&2
    echo "Keep traffic blocked and perform manual recovery." >&2
  fi
  set -e
  return "$rollback_failed"
}

on_exit() {
  local exit_code=$?
  stop_canary
  if [[ "$exit_code" -ne 0 \
     && "$mutation_started" == "true" \
     && "$activation_succeeded" != "true" ]]; then
    rollback_failed_activation || true
  fi
  sudo -n rm -f -- \
    "${current_link}.new.$$" \
    "${previous_link}.new.$$" >/dev/null 2>&1 || true
  if [[ -n "$unit_backup_dir" && -d "$unit_backup_dir" ]]; then
    sudo -n rm -rf -- "$unit_backup_dir" >/dev/null 2>&1 || true
  fi
  exit "$exit_code"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ ! -d "$release_dir" || ! -x "$release_dir/venv/bin/python" ]]; then
  echo "Installed Agent release is incomplete: $release_dir" >&2
  exit 1
fi
metadata="$(
  "$release_dir/venv/bin/python" \
    "$release_dir/deploy/ec2-agent/verify_release_artifact.py" \
    --installed-dir "$release_dir"
)"
read -r manifest_release_id manifest_commit_sha <<<"$metadata"
if [[ "$manifest_release_id" != "$release_id" ]]; then
  echo "Activation target does not match its release manifest." >&2
  exit 1
fi
if ! grep -Fxq "APP_RELEASE_VERSION=$release_id" "$release_dir/release.env" \
   || ! grep -Fxq "AGENT_RELEASE_COMMIT_SHA=$manifest_commit_sha" "$release_dir/release.env"; then
  echo "Release environment does not match the verified manifest metadata." >&2
  exit 1
fi

sudo -n "$release_dir/venv/bin/python" \
  "$release_dir/deploy/ec2-agent/validate_env.py" \
  "$common_env_file" \
  "$bedrock_env_file"

if [[ -L "$current_link" ]]; then
  previous_release="$(readlink -f "$current_link")"
  case "$previous_release" in
    "$base_dir"/releases/*) ;;
    *)
      echo "Current Agent release resolves outside the release directory." >&2
      exit 1
      ;;
  esac
fi

for unit in "${enable_units[@]}"; do
  if systemctl is-enabled --quiet "$unit"; then
    previous_enabled["$unit"]=true
  else
    previous_enabled["$unit"]=false
  fi
  if systemctl is-active --quiet "$unit"; then
    previous_active["$unit"]=true
  else
    previous_active["$unit"]=false
  fi
done

echo "Running release-specific migration under a transient unit."
sudo -n systemd-run \
  --quiet \
  --wait \
  --pipe \
  --collect \
  --unit="${canary_unit}-migrate" \
  --property=Type=oneshot \
  --property=User=ec2-user \
  --property=Group=ec2-user \
  --property="WorkingDirectory=$release_dir" \
  --property="EnvironmentFile=$common_env_file" \
  --property="EnvironmentFile=$release_dir/release.env" \
  --property=UnsetEnvironment=AWS_BEARER_TOKEN_BEDROCK \
  --setenv=DA_DRUG_SERVICE=agent_app \
  --setenv=AGENT_STARTUP_MIGRATIONS_ENABLED=false \
  --setenv=AGENT_EMBEDDED_WORKER_ENABLED=false \
  "$release_dir/venv/bin/python" -m agent_app.migrate

if ss -H -ltn | awk -v port=":${canary_port}" '$4 ~ (port "$") { found=1 } END { exit !found }'; then
  echo "Candidate port $canary_port is already listening." >&2
  exit 1
fi

echo "Starting isolated loopback release canary on port $canary_port."
sudo -n systemd-run \
  --quiet \
  --collect \
  --unit="$canary_unit" \
  --property=Type=simple \
  --property=User=ec2-user \
  --property=Group=ec2-user \
  --property="WorkingDirectory=$release_dir" \
  --property="EnvironmentFile=$common_env_file" \
  --property="EnvironmentFile=$release_dir/release.env" \
  --property="EnvironmentFile=-$bedrock_env_file" \
  --setenv=DA_DRUG_SERVICE=agent_app \
  --setenv=AGENT_STARTUP_MIGRATIONS_ENABLED=false \
  --setenv=AGENT_EMBEDDED_WORKER_ENABLED=false \
  "$release_dir/venv/bin/uvicorn" agent_app.main:app \
  --host 127.0.0.1 \
  --port "$canary_port" \
  --workers 1 \
  --no-access-log

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${canary_port}/health/ready" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "http://127.0.0.1:${canary_port}/health/ready" >/dev/null
"$release_dir/venv/bin/python" \
  "$release_dir/deploy/ec2-agent/probe_generation.py" \
  "$common_env_file" \
  --base-url "http://127.0.0.1:${canary_port}" \
  --timeout-seconds "${AGENT_GENERATION_VERIFY_TIMEOUT_SECONDS:-120}"
stop_canary

unit_backup_dir="$(sudo -n mktemp -d /run/dranswer-agent-units.XXXXXX)"
for unit in "${unit_names[@]}"; do
  if [[ -f "$service_dir/$unit" ]]; then
    sudo -n cp -a -- "$service_dir/$unit" "$unit_backup_dir/$unit"
  fi
done

mutation_started=true
sudo -n systemctl stop dranswer-agent-worker.service \
  dranswer-agent-langfuse-exporter.service \
  dranswer-agent-api.service >/dev/null 2>&1 || true
for unit in "${unit_names[@]}"; do
  sudo -n install -m 0644 -o root -g root \
    "$release_dir/deploy/ec2-agent/systemd/$unit" \
    "$service_dir/$unit"
done
atomic_link "$release_dir" "$current_link"
sudo -n systemctl daemon-reload
sudo -n systemctl restart dranswer-agent-migrate.service
sudo -n systemctl enable dranswer-agent-api.service \
  dranswer-agent-worker.service >/dev/null
sudo -n systemctl start dranswer-agent-api.service

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:8701/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "http://127.0.0.1:8701/health" >/dev/null
sudo -n systemctl start dranswer-agent-worker.service
if langfuse_enabled; then
  sudo -n systemctl enable dranswer-agent-langfuse-exporter.service >/dev/null
  sudo -n systemctl start dranswer-agent-langfuse-exporter.service
else
  sudo -n systemctl disable --now \
    dranswer-agent-langfuse-exporter.service >/dev/null 2>&1 || true
fi
sudo -n systemctl disable dranswer-agent-migrate.service >/dev/null 2>&1 || true

bash "$release_dir/deploy/ec2-agent/verify.sh"

if [[ -n "$previous_release" && "$previous_release" != "$release_dir" ]]; then
  atomic_link "$previous_release" "$previous_link"
fi
activation_succeeded=true
echo "Activated Agent release: $release_id ($manifest_commit_sha)"
echo "Previous application release: ${previous_release:-none}"
echo "Database backup/PITR reference: $AGENT_DB_BACKUP_ID"
