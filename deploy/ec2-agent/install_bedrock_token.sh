#!/usr/bin/env bash
set +x
set -Eeuo pipefail

env_dir="/etc/dranswer-agent"
secret_file="$env_dir/bedrock.env"
candidate_file="$env_dir/.bedrock.env.new.$$"
token=""

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  exit 1
}

cleanup() {
  unset token
  sudo -n rm -f -- "$candidate_file" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [[ $# -ne 0 ]]; then
  echo "Usage: provide the Bedrock bearer token on stdin; arguments are forbidden." >&2
  exit 2
fi

if [[ ! -d "$env_dir" ]]; then
  fail "Agent environment directory is not installed."
fi

# Never let sudo consume secret stdin. EC2's service account normally has
# passwordless sudo; otherwise authenticate separately with `sudo -v` first.
if ! sudo -n -v; then
  fail "sudo authentication is required; run sudo -v before retrying."
fi

if [[ -t 0 ]]; then
  printf 'Bedrock bearer token (hidden): ' >&2
fi
if ! IFS= read -r -s token; then
  if [[ -z "$token" ]]; then
    fail "Bedrock bearer token is empty."
  fi
fi
if [[ -t 0 ]]; then
  printf '\n' >&2
fi

if [[ ! -t 0 ]]; then
  _extra_input=""
  if IFS= read -r _extra_input || [[ -n "$_extra_input" ]]; then
    fail "Bedrock bearer token input must contain exactly one line."
  fi
fi
if [[ -z "$token" ]]; then
  fail "Bedrock bearer token is empty."
fi
if [[ "$token" =~ [[:space:]] || "$token" == *"'"* ]]; then
  fail "Bedrock bearer token contains unsupported characters."
fi
if [[ "$token" == *CHANGE_ME* || "$token" == *'<'* || "$token" == *'>'* ]]; then
  fail "Bedrock bearer token is still a placeholder."
fi

# A single-quoted systemd EnvironmentFile value preserves every accepted
# character. The secret flows through stdin and a protected root-owned file;
# it is never placed in argv or printed.
printf "AWS_BEARER_TOKEN_BEDROCK='%s'\n" "$token" |
  sudo -n install \
    -m 0600 \
    -o root \
    -g root \
    /dev/stdin \
    "$candidate_file"
unset token
sudo -n mv -f -- "$candidate_file" "$secret_file"
sudo -n chown root:root "$secret_file"
sudo -n chmod 0600 "$secret_file"

trap - EXIT
echo "Installed Bedrock credential file without printing the secret."
