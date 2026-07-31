#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/dranswer-agent-contract-<release>.tar.gz" >&2
  exit 2
fi

archive="$(readlink -f "$1")"
if [[ ! -f "$archive" ]]; then
  echo "Archive not found: $archive" >&2
  exit 2
fi
checksum_file="${archive}.sha256"
if [[ ! -f "$checksum_file" ]]; then
  echo "Checksum file not found: $checksum_file" >&2
  exit 2
fi

(
  cd "$(dirname "$archive")"
  sha256sum -c "$(basename "$checksum_file")"
)

for command_name in curl python3.11 openssl tar; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "Required command is missing: $command_name" >&2
    exit 1
  fi
done

archive_name="$(basename "$archive")"
if [[ "$archive_name" =~ ^dranswer-agent-contract-([0-9A-Za-z._-]+)\.tar\.gz$ ]]; then
  release_id="${BASH_REMATCH[1]}"
else
  echo "Archive name does not contain a valid release ID: $archive_name" >&2
  exit 2
fi
if [[ ! "$release_id" =~ ^[0-9A-Za-z._-]+$ ]]; then
  echo "RELEASE_ID contains unsupported characters." >&2
  exit 2
fi
base_dir="/opt/dranswer-agent-contract"
release_dir="$base_dir/releases/$release_id"
candidate_link="$base_dir/candidate"
env_dir="/etc/dranswer-agent-contract"
env_file="$env_dir/contract.env"
migration_env_file="$env_dir/contract-migration.env"
service_user="dranswer-contract"
service_group="dranswer-contract"

if [[ -e "$release_dir" ]]; then
  echo "Release already exists: $release_dir" >&2
  exit 1
fi

if ! getent group "$service_group" >/dev/null 2>&1; then
  sudo groupadd --system "$service_group"
fi
if ! id "$service_user" >/dev/null 2>&1; then
  sudo useradd \
    --system \
    --gid "$service_group" \
    --home-dir /nonexistent \
    --shell /sbin/nologin \
    "$service_user"
fi

sudo install -d -m 0755 -o root -g root \
  "$base_dir" "$base_dir/releases" "$release_dir"
sudo install -d -m 0750 -o root -g "$service_group" "$env_dir"
sudo tar -xzf "$archive" -C "$release_dir"
sudo chmod -R u=rwX,go=rX "$release_dir"
sudo chmod 0755 \
  "$release_dir/deploy/ec2-contract-server/activate.sh" \
  "$release_dir/deploy/ec2-contract-server/install_release.sh" \
  "$release_dir/deploy/ec2-contract-server/verify.sh"

sudo python3.11 -m venv "$release_dir/.venv"
sudo "$release_dir/.venv/bin/python" -m pip install \
  --disable-pip-version-check \
  --no-input \
  -r "$release_dir/deploy/ec2-contract-server/requirements.contract.lock"

if ! sudo test -f "$env_file"; then
  agent_token="$(openssl rand -hex 32)"
  control_token="$(openssl rand -hex 32)"
  feedback_digest_secret="$(openssl rand -hex 32)"
  sudo install \
    -m 0640 \
    -o root \
    -g "$service_group" \
    "$release_dir/deploy/ec2-contract-server/.env.contract.example" \
    "$env_file"
  sudo sed -i \
    "s/^AGENT_SYNC_API_TOKEN=.*/AGENT_SYNC_API_TOKEN=${agent_token}/" \
    "$env_file"
  sudo sed -i \
    "s/^TEST_CONTROL_TOKEN=.*/TEST_CONTROL_TOKEN=${control_token}/" \
    "$env_file"
  sudo sed -i \
    "s/^FEEDBACK_DIGEST_SECRET=.*/FEEDBACK_DIGEST_SECRET=${feedback_digest_secret}/" \
    "$env_file"
  env_created=1
else
  env_created=0
  if ! sudo grep -q '^FEEDBACK_DIGEST_SECRET=.\{32,\}$' "$env_file"; then
    feedback_digest_secret="$(openssl rand -hex 32)"
    printf 'FEEDBACK_DIGEST_SECRET=%s\n' "$feedback_digest_secret" |
      sudo tee -a "$env_file" >/dev/null
  fi
fi
if ! sudo test -f "$migration_env_file"; then
  sudo install \
    -m 0600 \
    -o root \
    -g root \
    "$release_dir/deploy/ec2-contract-server/.env.contract-migration.example" \
    "$migration_env_file"
  migration_env_created=1
else
  migration_env_created=0
fi

candidate_tmp="$base_dir/.candidate-${release_id}"
sudo ln -s "$release_dir" "$candidate_tmp"
sudo mv -Tf "$candidate_tmp" "$candidate_link"

echo "Installed contract-server candidate: $release_id"
echo "Candidate release: $release_dir"
if (( env_created == 1 )); then
  echo "Generated API, test-control, and feedback digest secrets in $env_file."
else
  echo "Kept existing environment file: $env_file"
fi
if (( migration_env_created == 1 )); then
  echo "Created root-only migration environment: $migration_env_file"
else
  echo "Kept root-only migration environment: $migration_env_file"
fi
echo "No service was started."
echo "Activate with:"
echo "  bash $candidate_link/deploy/ec2-contract-server/activate.sh"
