#!/usr/bin/env bash
set -Eeuo pipefail

base_dir="/opt/dranswer-agent-contract"
current_link="$base_dir/current"
env_file="/etc/dranswer-agent-contract/contract.env"
migration_env_file="/etc/dranswer-agent-contract/contract-migration.env"
release_env_file="/etc/dranswer-agent-contract/release.env"
api_unit="dranswer-agent-contract-api.service"
callback_unit="dranswer-agent-contract-callback.service"
service_group="dranswer-contract"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
candidate_dir="$(cd "$script_dir/../.." && pwd -P)"
case "$candidate_dir" in
  "$base_dir"/releases/*) ;;
  *)
    echo "Candidate is outside the release directory: $candidate_dir" >&2
    exit 2
    ;;
esac
release_id="$(basename "$candidate_dir")"
if [[ ! "$release_id" =~ ^[0-9A-Za-z._-]+$ ]]; then
  echo "Candidate release ID is invalid." >&2
  exit 2
fi

if [[ ! -x "$candidate_dir/.venv/bin/python" ]]; then
  echo "Candidate Python is missing." >&2
  exit 1
fi
if ! sudo test -f "$env_file"; then
  echo "Contract server environment is missing." >&2
  exit 1
fi
if ! sudo test -f "$migration_env_file"; then
  echo "Contract migration environment is missing." >&2
  exit 1
fi
for contract_file in \
  AI_V13_CHAT_OPENAPI.json \
  AI_V13_ASYNC_MEDICATION_OPENAPI.json \
  BACKEND_V13_ASYNC_CALLBACK_OPENAPI.json
do
  if [[ ! -f "$candidate_dir/docs/$contract_file" ]]; then
    echo "Candidate contract artifact is missing: $contract_file" >&2
    exit 1
  fi
done

previous_dir=""
previous_release_id=""
if [[ -L "$current_link" ]]; then
  previous_dir="$(readlink -f "$current_link")"
  previous_release_id="$(basename "$previous_dir")"
fi
api_was_active=0
worker_was_active=0
api_was_enabled=0
worker_was_enabled=0
if sudo systemctl is-active --quiet "$api_unit"; then
  api_was_active=1
fi
if sudo systemctl is-active --quiet "$callback_unit"; then
  worker_was_active=1
fi
if sudo systemctl is-enabled --quiet "$api_unit"; then
  api_was_enabled=1
fi
if sudo systemctl is-enabled --quiet "$callback_unit"; then
  worker_was_enabled=1
fi

switched=0

restore_previous() {
  local exit_code=$?
  trap - ERR
  set +e
  echo "Activation failed; restoring the previous contract-server state." >&2
  sudo systemctl stop "$callback_unit" >/dev/null 2>&1
  if (( switched == 1 )); then
    if [[ -n "$previous_dir" && -d "$previous_dir" ]]; then
      rollback_tmp="$base_dir/.current-rollback-${previous_release_id}-$$"
      sudo ln -s "$previous_dir" "$rollback_tmp"
      sudo mv -Tf "$rollback_tmp" "$current_link"
      printf 'APP_RELEASE_VERSION=%s\n' "$previous_release_id" |
        sudo install \
          -m 0640 \
          -o root \
          -g "$service_group" \
          /dev/stdin \
          "$release_env_file"
      for unit in \
        "$previous_dir"/deploy/ec2-contract-server/systemd/*.service
      do
        sudo install -m 0644 -o root -g root "$unit" /etc/systemd/system/
      done
      sudo systemctl daemon-reload
      if (( api_was_enabled == 1 )); then
        sudo systemctl enable "$api_unit" >/dev/null 2>&1
      else
        sudo systemctl disable "$api_unit" >/dev/null 2>&1
      fi
      if (( api_was_active == 1 )); then
        sudo systemctl restart "$api_unit"
      else
        sudo systemctl stop "$api_unit" >/dev/null 2>&1
      fi
    else
      sudo systemctl disable --now "$api_unit" >/dev/null 2>&1
      sudo unlink "$current_link" >/dev/null 2>&1
      sudo unlink "$release_env_file" >/dev/null 2>&1
    fi
  fi
  if (( worker_was_enabled == 1 )); then
    sudo systemctl enable "$callback_unit" >/dev/null 2>&1
  else
    sudo systemctl disable "$callback_unit" >/dev/null 2>&1
  fi
  if (( worker_was_active == 1 )); then
    sudo systemctl start "$callback_unit"
  fi
  exit "$exit_code"
}
trap restore_previous ERR

# Stop outbound side effects before candidate validation or any code switch.
sudo systemctl stop "$callback_unit" >/dev/null 2>&1 || true

sudo bash -c "
  set -a
  source '$env_file'
  source '$migration_env_file'
  set +a
  export APP_RELEASE_VERSION='$release_id'
  export CONTRACT_SPECS_DIR='$candidate_dir/docs'
  cd '$candidate_dir'
  '$candidate_dir/.venv/bin/python' -c \"
from contract_test_server.config import ContractServerSettings
s = ContractServerSettings()
s.require_contract_postgresql()
s.require_contract_migration_postgresql()
errors = s.readiness_errors
assert not errors, ','.join(errors)
assert str(s.contract_specs_dir).startswith('/opt/dranswer-agent-contract/')
\"
  exec '$candidate_dir/.venv/bin/python' \
    -m contract_test_server.migrate
"

for unit in \
  "$candidate_dir"/deploy/ec2-contract-server/systemd/*.service
do
  sudo install -m 0644 -o root -g root "$unit" /etc/systemd/system/
done

current_tmp="$base_dir/.current-${release_id}-$$"
sudo ln -s "$candidate_dir" "$current_tmp"
sudo mv -Tf "$current_tmp" "$current_link"
switched=1
printf 'APP_RELEASE_VERSION=%s\n' "$release_id" |
  sudo install \
    -m 0640 \
    -o root \
    -g "$service_group" \
    /dev/stdin \
    "$release_env_file"

sudo systemctl daemon-reload
sudo systemctl enable "$api_unit"
sudo systemctl restart "$api_unit"

"$candidate_dir/deploy/ec2-contract-server/verify.sh" \
  "$release_id" \
  stopped

callback_mode="$(
  sudo awk -F= '$1 == "CALLBACK_MODE" { print $2 }' "$env_file"
)"
if [[ "$callback_mode" == "deliver" ]]; then
  sudo systemctl enable "$callback_unit"
  sudo systemctl restart "$callback_unit"
  expected_worker_state="running"
else
  sudo systemctl disable "$callback_unit" >/dev/null 2>&1 || true
  expected_worker_state="stopped"
fi

"$candidate_dir/deploy/ec2-contract-server/verify.sh" \
  "$release_id" \
  "$expected_worker_state"

trap - ERR
echo "Activated contract-server release: $release_id"
