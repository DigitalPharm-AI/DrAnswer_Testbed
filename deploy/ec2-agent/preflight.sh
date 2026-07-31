#!/usr/bin/env bash
set -Eeuo pipefail

AGENT_PORT="${AGENT_PORT:-8701}"
MIN_DISK_KB=$((4 * 1024 * 1024))
MIN_MEMORY_KB=$((768 * 1024))
failed=0

ok() {
  printf 'OK   %s\n' "$1"
}

warn() {
  printf 'WARN %s\n' "$1" >&2
}

fail() {
  printf 'FAIL %s\n' "$1" >&2
  failed=1
}

for command_name in python3.11 curl openssl tar sha256sum systemctl systemd-run ss flock; do
  if command -v "$command_name" >/dev/null 2>&1; then
    ok "$command_name available"
  else
    fail "$command_name missing"
  fi
done

if command -v python3.11 >/dev/null 2>&1; then
  python_version="$(python3.11 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')"
  if python3.11 -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
    ok "Python $python_version"
  else
    fail "Python 3.11+ required; found $python_version"
  fi
  if python3.11 -m venv --help >/dev/null 2>&1; then
    ok "Python venv available"
  else
    fail "Python venv module unavailable"
  fi
fi

if ss -H -ltn | awk -v port=":${AGENT_PORT}" '$4 ~ (port "$") { found=1 } END { exit !found }'; then
  if [[ "${ALLOW_EXISTING_AGENT_UPGRADE:-false}" == "true" ]] \
     && systemctl is-active --quiet dranswer-agent-api.service; then
    agent_pid="$(
      systemctl show \
        --property=MainPID \
        --value \
        dranswer-agent-api.service
    )"
    listener_details="$(ss -H -ltnp 2>/dev/null || true)"
    if [[ "$agent_pid" =~ ^[1-9][0-9]*$ ]] \
       && grep -Eq "pid=${agent_pid}([,)]|$)" <<<"$listener_details"; then
      ok "TCP port $AGENT_PORT is held by the active Agent API upgrade target"
    else
      fail "TCP port $AGENT_PORT listener does not match dranswer-agent-api.service"
    fi
  else
    fail "TCP port $AGENT_PORT is already listening"
  fi
else
  ok "TCP port $AGENT_PORT is free"
fi

memory_kb="$(awk '/^MemTotal:/ { print $2 }' /proc/meminfo)"
if (( memory_kb < MIN_MEMORY_KB )); then
  fail "memory below 768 MiB"
else
  ok "memory $((memory_kb / 1024)) MiB"
fi

swap_kb="$(awk '/^SwapTotal:/ { print $2 }' /proc/meminfo)"
if (( swap_kb == 0 && memory_kb < 2 * 1024 * 1024 )); then
  warn "no swap on a sub-2 GiB host; add 1 GiB swap before dependency install"
else
  ok "swap $((swap_kb / 1024)) MiB"
fi

available_kb="$(df -Pk /opt 2>/dev/null | awk 'NR == 2 { print $4 }')"
if [[ -z "$available_kb" ]]; then
  available_kb="$(df -Pk / | awk 'NR == 2 { print $4 }')"
fi
if (( available_kb < MIN_DISK_KB )); then
  fail "less than 4 GiB free disk"
else
  ok "free disk $((available_kb / 1024)) MiB"
fi

if systemctl is-active --quiet chat-server.service; then
  warn "existing chat-server.service is active; it will not be modified"
fi

if (( failed != 0 )); then
  exit 1
fi

printf 'Host preflight: ready for agent package installation\n'
