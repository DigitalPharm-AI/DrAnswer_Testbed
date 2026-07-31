#!/usr/bin/env bash
set -Eeuo pipefail

swap_file="/swapfile"
swap_size_gb="${SWAP_SIZE_GB:-1}"
base_dir="/opt/dranswer-agent"
env_dir="/etc/dranswer-agent"

if ! [[ "$swap_size_gb" =~ ^[1-9][0-9]*$ ]]; then
  echo "SWAP_SIZE_GB must be a positive integer." >&2
  exit 2
fi

if swapon --show=NAME --noheadings | grep -Fxq "$swap_file"; then
  echo "Swap already active: $swap_file"
elif [[ -e "$swap_file" ]]; then
  echo "Refusing to overwrite existing inactive $swap_file" >&2
  exit 1
else
  echo "Creating ${swap_size_gb} GiB swap at $swap_file"
  sudo fallocate -l "${swap_size_gb}G" "$swap_file"
  sudo chmod 600 "$swap_file"
  sudo mkswap "$swap_file"
  sudo swapon "$swap_file"
fi

if ! grep -Eq '^[[:space:]]*/swapfile[[:space:]]' /etc/fstab; then
  printf '%s\n' '/swapfile none swap sw 0 0' \
    | sudo tee -a /etc/fstab >/dev/null
fi

sudo install -d -m 0755 -o root -g root \
  "$base_dir" "$base_dir/releases"
sudo install -d -m 0700 -o ec2-user -g ec2-user \
  "$base_dir/runtime"
if [[ ! -e "$base_dir/deploy.lock" ]]; then
  sudo install -m 0660 -o root -g ec2-user /dev/null \
    "$base_dir/deploy.lock"
fi
sudo install -d -m 0750 -o root -g ec2-user "$env_dir"

echo "Host preparation complete."
free -h
df -h /
