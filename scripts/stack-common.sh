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

stack_load_tts_api_key() {
  if [[ -n "${TTS_API_KEY:-}" ]]; then
    export TTS_API_KEY
    return 0
  fi
  if [[ -x /usr/bin/security ]]; then
    TTS_API_KEY="$(/usr/bin/security find-generic-password -a "$USER" -s "${TTS_KEYCHAIN_SERVICE:-com.ufun.tts.api-key}" -w 2>/dev/null || true)"
    export TTS_API_KEY
  fi
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

stack_adopt_pid() {
  local port="$1"
  local marker="$2"
  local pid_file="$3"
  local pid
  pid="$(stack_find_project_pid "$port" "$marker" || true)"
  if [[ -n "$pid" ]]; then
    printf '%s\n' "$pid" >"$pid_file"
    return 0
  fi
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

stack_stop_project_process() {
  local label="$1"
  local port="$2"
  local marker="$3"
  local pid_file="$4"
  local pid=""
  local i

  if [[ -s "$pid_file" ]]; then
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ ! "$pid" =~ ^[0-9]+$ ]] || ! /bin/kill -0 "$pid" 2>/dev/null || ! stack_process_matches "$pid" "$marker"; then
      pid=""
      rm -f "$pid_file"
    fi
  fi

  if [[ -z "$pid" ]]; then
    pid="$(stack_find_project_pid "$port" "$marker" || true)"
  fi

  if [[ -z "$pid" ]]; then
    if [[ -n "$(stack_port_pids "$port")" ]]; then
      echo "$label: listener on port $port is not managed by this repository; leaving it untouched."
    else
      echo "$label: already stopped"
    fi
    rm -f "$pid_file"
    return 0
  fi

  /bin/kill "$pid" 2>/dev/null || true
  for ((i = 1; i <= 20; i++)); do
    if ! /bin/kill -0 "$pid" 2>/dev/null; then
      break
    fi
    sleep 0.25
  done
  if /bin/kill -0 "$pid" 2>/dev/null; then
    /bin/kill -KILL "$pid" 2>/dev/null || true
  fi
  rm -f "$pid_file"
  echo "$label: stopped"
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
