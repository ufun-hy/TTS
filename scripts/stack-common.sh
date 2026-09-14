#!/usr/bin/env bash
# Shared helpers for the local TTS development stack.

STACK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STACK_RUNTIME="$STACK_ROOT/runtime"
STACK_PID_DIR="$STACK_RUNTIME/pids"
STACK_LOG_DIR="$STACK_RUNTIME/logs"

mkdir -p "$STACK_PID_DIR" "$STACK_LOG_DIR"

stack_http_ok() {
  /usr/bin/curl -fsS --max-time 2 "$1" >/dev/null 2>&1
}

stack_wait_http() {
  local url="$1"
  local attempts="${2:-30}"
  local i
  for ((i = 1; i <= attempts; i++)); do
    if stack_http_ok "$url"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

stack_port_pids() {
  local port="$1"
  /usr/sbin/lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null || true
}

stack_process_matches() {
  local pid="$1"
  local marker="$2"
  local command
  command="$(/bin/ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ -n "$command" && "$command" == *"$STACK_ROOT"* && "$command" == *"$marker"* ]]
}

stack_find_project_pid() {
  local port="$1"
  local marker="$2"
  local pid
  for pid in $(stack_port_pids "$port"); do
    if stack_process_matches "$pid" "$marker"; then
      printf '%s\n' "$pid"
      return 0
    fi
  done
  return 1
}

stack_port_has_unknown_listener() {
  local port="$1"
  local marker="$2"
  local found=0
  local pid
  for pid in $(stack_port_pids "$port"); do
    found=1
    if ! stack_process_matches "$pid" "$marker"; then
      return 0
    fi
  done
  [[ "$found" -eq 0 ]] && return 1
  return 1
}

stack_lan_ip() {
  local interface=""
  local candidate
  interface="$(/sbin/route -n get default 2>/dev/null | /usr/bin/awk '/interface:/{print $2; exit}' || true)"
  if [[ -n "$interface" ]]; then
    candidate="$(/usr/sbin/ipconfig getifaddr "$interface" 2>/dev/null || true)"
    if [[ -n "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  fi
  for interface in en0 en1; do
    candidate="$(/usr/sbin/ipconfig getifaddr "$interface" 2>/dev/null || true)"
    if [[ -n "$candidate" ]]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  return 1
}
