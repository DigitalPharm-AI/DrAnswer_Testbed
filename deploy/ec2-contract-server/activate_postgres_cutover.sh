#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $EUID -ne 0 ]]; then
  exec sudo "$0" "$@"
fi
if [[ $# -ne 2 ]]; then
  echo "Usage: $0 <release-id> <backup-directory>" >&2
  exit 2
fi

release_id="$1"
backup_dir="$(readlink -f "$2")"
base_dir="/opt/dranswer-agent-contract"
release_dir="$base_dir/releases/$release_id"
current_link="$base_dir/current"
env_file="/etc/dranswer-agent-contract/contract.env"
release_env_file="/etc/dranswer-agent-contract/release.env"
api_unit="dranswer-agent-contract-api.service"
callback_unit="dranswer-agent-contract-callback.service"

if [[ ! "$release_id" =~ ^[0-9A-Za-z._-]+$ ]]; then
  echo "Release ID contains unsupported characters." >&2
  exit 2
fi
if [[ ! -d "$release_dir" ]]; then
  echo "Candidate release does not exist." >&2
  exit 1
fi
if [[ ! -d "$backup_dir" ]]; then
  echo "Cutover backup directory does not exist." >&2
  exit 1
fi
for backup_name in \
  contract.pg.env.pre-cutover \
  release.env \
  dranswer-agent-contract-api.service \
  dranswer-agent-contract-callback.service
do
  if [[ ! -f "$backup_dir/$backup_name" ]]; then
    echo "Required rollback artifact is missing: $backup_name" >&2
    exit 1
  fi
done
if systemctl is-active --quiet "$api_unit"; then
  echo "Contract API must be stopped before PostgreSQL cutover." >&2
  exit 1
fi
if systemctl is-active --quiet "$callback_unit"; then
  echo "Callback worker must be stopped before PostgreSQL cutover." >&2
  exit 1
fi

previous_dir="$(readlink -f "$current_link")"
previous_release_id="$(basename "$previous_dir")"
switched=0
units_installed=0

restore_sqlite_release() {
  local exit_code=$?
  trap - ERR
  set +e
  echo "Cutover activation failed; restoring SQLite application release." >&2
  systemctl stop "$callback_unit" >/dev/null 2>&1
  systemctl stop "$api_unit" >/dev/null 2>&1

  if (( switched == 1 )); then
    rollback_link="$base_dir/.current-rollback-${previous_release_id}-$$"
    ln -s "$previous_dir" "$rollback_link"
    mv -Tf "$rollback_link" "$current_link"
  fi
  if (( units_installed == 1 )); then
    install \
      -m 0644 \
      -o root \
      -g root \
      "$backup_dir/dranswer-agent-contract-api.service" \
      /etc/systemd/system/dranswer-agent-contract-api.service
    install \
      -m 0644 \
      -o root \
      -g root \
      "$backup_dir/dranswer-agent-contract-callback.service" \
      /etc/systemd/system/dranswer-agent-contract-callback.service
  fi
  install \
    -m 0640 \
    -o root \
    -g dranswer-contract \
    "$backup_dir/contract.pg.env.pre-cutover" \
    "$env_file"
  install \
    -m 0640 \
    -o root \
    -g dranswer-contract \
    "$backup_dir/release.env" \
    "$release_env_file"
  systemctl daemon-reload
  systemctl enable "$api_unit" >/dev/null 2>&1
  systemctl restart "$api_unit"
  systemctl disable "$callback_unit" >/dev/null 2>&1
  exit "$exit_code"
}
trap restore_sqlite_release ERR

sed -i \
  "s/^CONTRACT_HOST=.*/CONTRACT_HOST=127.0.0.1/" \
  "$env_file"

install \
  -m 0644 \
  -o root \
  -g root \
  "$release_dir/deploy/ec2-contract-server/systemd/$api_unit" \
  "/etc/systemd/system/$api_unit"
install \
  -m 0644 \
  -o root \
  -g root \
  "$release_dir/deploy/ec2-contract-server/systemd/$callback_unit" \
  "/etc/systemd/system/$callback_unit"
units_installed=1

current_temporary="$base_dir/.current-${release_id}-$$"
ln -s "$release_dir" "$current_temporary"
mv -Tf "$current_temporary" "$current_link"
switched=1
printf 'APP_RELEASE_VERSION=%s\n' "$release_id" |
  install \
    -m 0640 \
    -o root \
    -g dranswer-contract \
    /dev/stdin \
    "$release_env_file"

systemctl daemon-reload
systemctl enable "$api_unit"
systemctl disable "$callback_unit" >/dev/null 2>&1
systemctl start "$api_unit"
"$release_dir/deploy/ec2-contract-server/verify.sh" \
  "$release_id" \
  stopped

trap - ERR
echo "Activated PostgreSQL candidate on loopback: $release_id"
