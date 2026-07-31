#!/usr/bin/env bash
set -Eeuo pipefail

base_dir="/opt/dranswer-agent"
current_link="$base_dir/current"
previous_link="$base_dir/previous"

if [[ "${AGENT_SCHEMA_ROLLBACK_COMPATIBLE:-false}" != "true" ]]; then
  echo "Application rollback does not downgrade the Agent database." >&2
  echo "Set AGENT_SCHEMA_ROLLBACK_COMPATIBLE=true only after confirming" >&2
  echo "the target application supports the current database schema." >&2
  exit 1
fi
if [[ -z "${AGENT_DB_BACKUP_ID:-}" \
   || "${AGENT_DB_BACKUP_ID}" == *CHANGE_ME* ]]; then
  echo "AGENT_DB_BACKUP_ID must identify the retained backup/PITR point." >&2
  exit 1
fi
if [[ ! "${AGENT_DB_BACKUP_ID}" =~ ^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$ ]]; then
  echo "AGENT_DB_BACKUP_ID contains unsupported characters." >&2
  exit 1
fi
if [[ ! -L "$current_link" ]]; then
  echo "No active Agent release is available to run rollback." >&2
  exit 1
fi

if [[ $# -gt 1 ]]; then
  echo "Usage: $0 [installed-release-id]" >&2
  exit 2
fi
if [[ $# -eq 1 ]]; then
  target_id="$1"
  if [[ ! "$target_id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$ ]]; then
    echo "Invalid rollback release ID." >&2
    exit 2
  fi
  target_dir="$base_dir/releases/$target_id"
else
  if [[ ! -L "$previous_link" ]]; then
    echo "No preserved previous Agent release is available." >&2
    exit 1
  fi
  target_dir="$(readlink -f "$previous_link")"
  case "$target_dir" in
    "$base_dir"/releases/*) ;;
    *)
      echo "Previous Agent release resolves outside the release directory." >&2
      exit 1
      ;;
  esac
  target_id="$(basename "$target_dir")"
fi
if [[ ! -d "$target_dir" || ! -x "$target_dir/venv/bin/python" ]]; then
  echo "Rollback target is not a complete installed release: $target_dir" >&2
  exit 1
fi

activation_script="$current_link/deploy/ec2-agent/activate.sh"
echo "Rolling back application code and its exact release-specific dependencies."
echo "Target release: $target_id"
echo "Database schema will not be downgraded."
export AGENT_SCHEMA_FORWARD_COMPATIBLE=true
exec bash "$activation_script" "$target_id"
