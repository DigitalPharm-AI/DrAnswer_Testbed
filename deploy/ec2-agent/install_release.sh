#!/usr/bin/env bash
set -Eeuo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/dranswer-agent-<release>.tar.gz" >&2
  exit 2
fi

archive="$1"
if [[ ! -f "$archive" ]]; then
  echo "Archive not found: $archive" >&2
  exit 2
fi
archive="$(cd "$(dirname "$archive")" && pwd)/$(basename "$archive")"
checksum_file="${archive}.sha256"
if [[ ! -f "$checksum_file" ]]; then
  echo "Checksum file not found: $checksum_file" >&2
  exit 2
fi

(
  cd "$(dirname "$archive")"
  sha256sum -c "$(basename "$checksum_file")"
)

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
artifact_metadata="$(
  python3.11 "$script_dir/verify_release_artifact.py" --archive "$archive"
)"
read -r release_id commit_sha <<<"$artifact_metadata"
if [[ -z "$release_id" || -z "$commit_sha" ]]; then
  echo "Release artifact metadata is incomplete." >&2
  exit 1
fi
expected_archive_name="dranswer-agent-${release_id}.tar.gz"
if [[ "$(basename "$archive")" != "$expected_archive_name" ]]; then
  echo "Archive name does not match manifest release ID." >&2
  exit 1
fi

base_dir="/opt/dranswer-agent"
release_dir="$base_dir/releases/$release_id"
runtime_dir="$base_dir/runtime"
lock_file="$base_dir/deploy.lock"
env_dir="/etc/dranswer-agent"
common_env_file="$env_dir/agent.env"
bedrock_env_file="$env_dir/bedrock.env"
staging_dir=""

cleanup() {
  if [[ -n "$staging_dir" && -d "$staging_dir" ]]; then
    sudo -n rm -rf -- "$staging_dir" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

sudo -n install -d -m 0755 -o root -g root \
  "$base_dir" "$base_dir/releases"
if [[ ! -e "$lock_file" ]]; then
  sudo -n install -m 0660 -o root -g ec2-user /dev/null "$lock_file"
fi
exec 9>"$lock_file"
if ! flock -n 9; then
  echo "Another Agent release install or activation is in progress." >&2
  exit 1
fi
if [[ -e "$release_dir" ]]; then
  echo "Release already exists: $release_dir" >&2
  exit 1
fi
sudo -n install -d -m 0700 -o ec2-user -g ec2-user "$runtime_dir"
staging_dir="$base_dir/releases/.staging-${release_id}.$$"
sudo -n install -d -m 0700 -o ec2-user -g ec2-user "$staging_dir"
tar -xzf "$archive" -C "$staging_dir"

manifest_release_id="$(
  python3.11 - "$staging_dir/release-manifest.json" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
print(str(manifest["release_id"]))
PY
)"
manifest_commit_sha="$(
  python3.11 - "$staging_dir/release-manifest.json" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
print(str(manifest["commit_sha"]))
PY
)"
if [[ "$manifest_release_id" != "$release_id" \
   || "$manifest_commit_sha" != "$commit_sha" ]]; then
  echo "Extracted manifest does not match verified artifact metadata." >&2
  exit 1
fi

python3.11 -m venv "$staging_dir/venv"
"$staging_dir/venv/bin/python" -m pip install \
  --disable-pip-version-check \
  -r "$staging_dir/deploy/ec2-agent/requirements.agent.lock"

cat >"$staging_dir/release.env" <<EOF
APP_RELEASE_VERSION=$release_id
AGENT_RELEASE_COMMIT_SHA=$commit_sha
EOF
chmod 0644 "$staging_dir/release.env"

sudo -n chown -R root:root "$staging_dir"
sudo -n chmod -R go-w "$staging_dir"
sudo -n mv -- "$staging_dir" "$release_dir"
staging_dir=""

sudo -n install -d -m 0750 -o root -g ec2-user "$env_dir"
if [[ ! -f "$common_env_file" ]]; then
  sudo -n install -m 0640 -o root -g ec2-user \
    "$release_dir/deploy/ec2-agent/.env.agent_app.ec2.example" \
    "$common_env_file"
  common_env_created=1
else
  common_env_created=0
fi
if [[ ! -f "$bedrock_env_file" ]]; then
  sudo -n install -m 0600 -o root -g root \
    "$release_dir/deploy/ec2-agent/.env.bedrock.ec2.example" \
    "$bedrock_env_file"
  bedrock_env_created=1
else
  bedrock_env_created=0
fi

echo "Installed inactive release: $release_id"
echo "Commit SHA: $commit_sha"
echo "Release directory: $release_dir"
echo "Release-specific dependency environment: $release_dir/venv"
if (( common_env_created == 1 )); then
  echo "Created common environment file: $common_env_file"
  echo "Replace every CHANGE_ME value before activation."
else
  echo "Kept existing common environment file: $common_env_file"
fi
if (( bedrock_env_created == 1 )); then
  echo "Created empty Bedrock credential file: $bedrock_env_file"
else
  echo "Kept existing Bedrock credential file: $bedrock_env_file"
fi
if grep -Eq '^[[:space:]]*(export[[:space:]]+)?AWS_BEARER_TOKEN_BEDROCK[[:space:]]*=' "$common_env_file"; then
  echo "WARNING: AWS_BEARER_TOKEN_BEDROCK remains in $common_env_file." >&2
  echo "Install it through install_bedrock_token.sh, then remove the common-file entry." >&2
fi
echo "The current release and running services were not changed."
echo "Bearer tokens must be installed through stdin with:"
echo "  bash $release_dir/deploy/ec2-agent/install_bedrock_token.sh"
echo "Activate only after the backup and schema-compatibility gates:"
echo "  AGENT_DB_BACKUP_ID=<backup-or-disposable-test-db-id> \\"
echo "  AGENT_SCHEMA_FORWARD_COMPATIBLE=true \\"
echo "  bash $release_dir/deploy/ec2-agent/activate.sh $release_id"
