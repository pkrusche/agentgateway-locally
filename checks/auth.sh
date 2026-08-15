#!/usr/bin/env bash
#
# Assert that every route on every listener rejects unauthenticated
# requests.
#
# The apiKey and basicAuth policies attach to a *listener*, not to
# individual routes, so this should hold for paths that don't exist as
# much as for the documented ones. That's the property worth testing:
# a single unauthenticated route is enough to enumerate the setup, and
# an unauthenticated *proxying* route would be a free ride on the
# provider keys.
#
#   checks/auth.sh
#   GATEWAY_HOST=192.168.64.3 checks/auth.sh    # the vmnet address
#   RUN_PY="python3.12 run.py" checks/auth.sh   # older python3 on PATH
#
# Env overrides: GATEWAY_HOST, LLM_PORT, MCP_PORT, UI_PORT, GATEWAY_KEY,
# UI_USER, UI_PASSWORD, RUN_PY.
#
# GATEWAY_KEY/UI_PASSWORD only exist so the positive controls can run
# where `pass` can't be reached; leave them unset normally, since an
# exported secret is visible to every child process.
#
# Exit status is 0 only if every check passed. Needs the gateway
# running (./run.py up).

set -u

HOST="${GATEWAY_HOST:-127.0.0.1}"
LLM_PORT="${LLM_PORT:-4000}"
MCP_PORT="${MCP_PORT:-3000}"
UI_PORT="${UI_PORT:-15000}"
# Word-split on purpose, so a multi-word command works — needed where
# the `python3` on PATH is older than 3.11 and run.py's own shebang
# can't run it:  RUN_PY="python3.12 run.py" checks/auth.sh
# shellcheck disable=SC2206
RUN_PY_CMD=(${RUN_PY:-./run.py})

cd "$(dirname "$0")/.." || exit 1

pass_count=0
fail_count=0
skip_count=0

ok()   { printf '  \033[32mPASS\033[0m %s\n' "$1"; pass_count=$((pass_count + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; fail_count=$((fail_count + 1)); }
skip() { printf '  \033[33mSKIP\033[0m %s\n' "$1"; skip_count=$((skip_count + 1)); }

# Status code of an unauthenticated request. "000" means no HTTP
# response at all (connection refused, timeout).
status() {
  local method="$1" url="$2"
  curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
    -X "$method" -H 'content-type: application/json' "$url" 2>/dev/null
}

# Paths that must not answer without credentials. The made-up ones are
# deliberate: they check that the policy covers the whole listener
# rather than an enumerated route list.
PATHS=(
  /
  /v1/models
  /v1/chat/completions
  /v1/messages
  /v1/completions
  /v1/embeddings
  /v1/responses
  /health
  /healthz
  /ready
  /readyz
  /live
  /livez
  /metrics
  /stats
  /debug
  /debug/pprof
  /admin
  /config
  /ui
  /ui/
  /mcp
  /sse
  /openapi.json
  /.env
  /nonexistent-route-check
)

METHODS=(GET POST OPTIONS)

# --- data plane: 4000 and 3000 ----------------------------------------
#
# Every path, every method, must come back 401/403. Anything else is a
# finding: 2xx/3xx is an open route, and even a 404 would mean the
# request reached routing without passing the apiKey policy.
check_data_plane() {
  local port="$1" label="$2" path method code
  echo
  echo "$label (${HOST}:${port}) — unauthenticated requests must be rejected"

  if [ "$(status GET "http://${HOST}:${port}/")" = "000" ]; then
    bad "${label}: nothing listening on ${HOST}:${port} — is the gateway up?"
    return
  fi

  for path in "${PATHS[@]}"; do
    for method in "${METHODS[@]}"; do
      code="$(status "$method" "http://${HOST}:${port}${path}")"
      case "$code" in
        401 | 403) ok "$method $path -> $code" ;;
        *)         bad "$method $path -> $code (expected 401/403)" ;;
      esac
    done
  done
}

# --- admin UI: 15000 --------------------------------------------------
#
# Same rule, with one difference: paths the UI gateway doesn't route at
# all answer 404 before the basicAuth policy is consulted. That's not a
# way in and discloses nothing, so it's accepted — but a 2xx/3xx never
# is.
check_ui() {
  local path method code
  echo
  echo "Admin UI (${HOST}:${UI_PORT}) — unauthenticated requests must be rejected"

  if [ "$(status GET "http://${HOST}:${UI_PORT}/")" = "000" ]; then
    bad "UI: nothing listening on ${HOST}:${UI_PORT} — is the gateway up?"
    return
  fi

  for path in "${PATHS[@]}" /api/config /api/logs /api/logs/1; do
    for method in "${METHODS[@]}"; do
      code="$(status "$method" "http://${HOST}:${UI_PORT}${path}")"
      case "$code" in
        401 | 403) ok "$method $path -> $code" ;;
        404)       ok "$method $path -> 404 (not routed)" ;;
        *)         bad "$method $path -> $code (expected 401/403, or 404 if unrouted)" ;;
      esac
    done
  done
}

# --- positive controls ------------------------------------------------
#
# Without these the checks above would pass just as happily against a
# gateway that rejects *everything* — a misconfigured policy, or the
# wrong container. So prove the credentials that should work do.
#
# The key is read into a shell variable and handed to curl through a
# config file on stdin, never as an argument: curl's argv is readable by
# any local process via `ps`. Same reasoning as env_args() in run.py.
# See docs/security.md#the-gateway-key-on-the-command-line.
check_positive_controls() {
  echo
  echo "Positive controls — correct credentials must be accepted"

  local key code
  key="${GATEWAY_KEY:-$("${RUN_PY_CMD[@]}" key 2>/dev/null)}"
  if [ -z "$key" ]; then
    skip "authenticated request to /v1/models (no key — '${RUN_PY_CMD[*]} key' failed; is pass unlocked? see RUN_PY)"
  else
    code="$(printf 'header = "Authorization: Bearer %s"\n' "$key" |
      curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
        --config - "http://${HOST}:${LLM_PORT}/v1/models" 2>/dev/null)"
    case "$code" in
      2*) ok "GET /v1/models with the gateway key -> $code" ;;
      *)  bad "GET /v1/models with the gateway key -> $code (expected 2xx; the negative checks above prove nothing if this fails)" ;;
    esac
  fi

  local user password
  user="${UI_USER:-${GATEWAY_UI_USER:-admin}}"
  password="${UI_PASSWORD:-$("${RUN_PY_CMD[@]}" ui-password 2>/dev/null)}"
  if [ -z "$password" ]; then
    skip "authenticated request to /ui/ (no password — '${RUN_PY_CMD[*]} ui-password' failed; is pass unlocked? see RUN_PY)"
  else
    code="$(printf 'user = "%s:%s"\n' "$user" "$password" |
      curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
        --config - "http://${HOST}:${UI_PORT}/ui/" 2>/dev/null)"
    case "$code" in
      2*) ok "GET /ui/ with basic auth -> $code" ;;
      *)  bad "GET /ui/ with basic auth -> $code (expected 2xx)" ;;
    esac
  fi

  # A wrong key must still be rejected — i.e. the policy compares the
  # key rather than merely checking a header is present.
  code="$(printf 'header = "Authorization: Bearer %s"\n' "agw-not-the-real-key" |
    curl -s -o /dev/null -w '%{http_code}' --max-time 5 \
      --config - "http://${HOST}:${LLM_PORT}/v1/models" 2>/dev/null)"
  case "$code" in
    401 | 403) ok "GET /v1/models with a wrong key -> $code" ;;
    *)         bad "GET /v1/models with a wrong key -> $code (expected 401/403)" ;;
  esac
}

command -v curl >/dev/null 2>&1 || { echo "error: curl not found" >&2; exit 1; }

check_data_plane "$LLM_PORT" "LLM data plane"
check_data_plane "$MCP_PORT" "MCP"
check_ui
check_positive_controls

echo
echo "${pass_count} passed, ${fail_count} failed, ${skip_count} skipped"
[ "$fail_count" -eq 0 ] || exit 1
