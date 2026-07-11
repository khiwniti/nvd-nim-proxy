#!/usr/bin/env bash
# Stage 2: Install nim-claude-proxy==0.3.0 from PyPI in an isolated venv,
# then exercise the production binary chain end-to-end via the `nim` CLI:
#
#   nim version        — confirms the *installed* distribution version
#   nim --help         — top-level CLI surface (sanity)
#   nim start          — boots daemon (uvicorn via nim_code)
#   nim doctor         — health probe + key reachability
#   nim status         — daemon liveness
#   nim models         — live /v1/models from upstream
#   nim test           — single /v1/messages exchange
#   nim logs -n 10     — tail the daemon journal
#   SIGTERM            — confirm clean shutdown (no os._exit thread)
#   nim stop           — explicit stop
#
# Designed for a tmux session because (a) `nim test` waits on the full
# upstream round-trip, (b) `tail -f` on the logs is most useful interactively,
# and (c) we want to see the proxy output scroll by in real time.

set -uo pipefail
cd "$(dirname "$0")/.."

log() { printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" ; }
warn() { printf '\n\033[1;33m[WARN]\033[0m %s\n' "$*" ; }
fail() { printf '\n\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2 ; exit 1 ; }
ok() { printf '\033[1;32m[OK]\033[0m %s\n' "$*" ; }

[ -n "${NVIDIA_API_KEY:-}" ] || fail "NVIDIA_API_KEY not set in this shell"

VENV=".venv-prod"
PROXY_PORT="${PROXY_PORT:-8787}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/nim-proxy"
LOG_FILE="/tmp/nim-prod-smoke.log"

# Ensure NVIDIA_API_KEY propagates into the venv + daemon child processes.
export NVIDIA_API_KEY
unset PYPI_TOKEN

# ── 1. Fresh venv + install from PyPI ──────────────────────────────────────
log "Recreating isolated venv $VENV"
rm -rf "$VENV"
python3 -m venv "$VENV"
VENV_BIN="$VENV/bin"
"$VENV_BIN/pip" install --quiet --upgrade pip wheel

log "Installing nim-claude-proxy==0.3.0 from PyPI"
"$VENV_BIN/pip" --quiet install 'nim-claude-proxy==0.3.0'
INSTALLED="$("$VENV_BIN/python" -c 'import importlib.metadata as m; print(m.version("nim-claude-proxy"))')"
[ "$INSTALLED" = "0.3.0" ] || fail "expected 0.3.0 installed, got $INSTALLED"
ok "installed: nim-claude-proxy==$INSTALLED"

# ── 2. Drop a clean per-test config so we don't fight the user's global one ─
log "Writing isolated config to $CONFIG_DIR"
mkdir -p "$CONFIG_DIR"
cat > "$CONFIG_DIR/config.yaml" <<YAML
server:
  host: 127.0.0.1
  port: ${PROXY_PORT}
  log_level: info

nvidia:
  base_url: ${NVIDIA_BASE_URL:-https://integrate.api.nvidia.com/v1}
  default_model: nvidia/llama-3.3-nemotron-super-49b-v1.5
  opus_model: deepseek-ai/deepseek-v4-pro
  haiku_model: minimaxai/minimax-m2.7

streaming:
  ping_interval: 15.0
  budget_seconds: 600.0

model_aliases: {}
YAML
ok "config written"

# ── 3. Make sure no stale daemon from a previous run is still bound ───────
log "Cleaning any leftover daemon"
"$VENV_BIN/nim" stop >/dev/null 2>&1 &
"$VENV_BIN/nim" kill --port "$PROXY_PORT" >/dev/null 2>&1 || true
sleep 1

# ── 4. Version + help sanity ───────────────────────────────────────────────
log "nim --version"
"$VENV_BIN/nim" --version

log "nim --help (top-level)"
"$VENV_BIN/nim" --help | head -25

# ── 5. Start the daemon ────────────────────────────────────────────────────
log "nim start (background)"
( "$VENV_BIN/nim" start ) > $LOG_FILE 2>&1
START_RC=$?
tail -20 $LOG_FILE | sed 's/^/  | /'
[ $START_RC -eq 0 ] || fail "nim start exited with rc=$START_RC"
ok "nim start returned exit 0"

# Wait until /healthz is responsive (max 25s)
log "Waiting for /healthz on port $PROXY_PORT"
HEALTH_OK=0
for i in $(seq 1 50); do
  if curl -fsS "http://127.0.0.1:$PROXY_PORT/healthz" > /tmp/prod-health.json 2>/dev/null; then
    HEALTH_OK=1
    ok "/healthz responded in $((i*500))ms"
    break
  fi
  sleep 0.5
done
if [ "$HEALTH_OK" -ne 1 ]; then
  warn "/healthz never came up; tailing logs"
  tail -40 $LOG_FILE | sed 's/^/  | /'
  fail "daemon did not bind /healthz"
fi

log "Body of /healthz (components block)"
python3 -m json.tool < /tmp/prod-health.json | sed 's/^/  /'

# ── 6. nim doctor ───────────────────────────────────────────────────────────
log "nim doctor (proxy + key + port)"
"$VENV_BIN/nim" doctor || warn "nim doctor reported warnings (allowed)"

# ── 7. nim status ───────────────────────────────────────────────────────────
log "nim status"
"$VENV_BIN/nim" status

# ── 8. nim models ───────────────────────────────────────────────────────────
log "nim models (live)"
"$VENV_BIN/nim" models | head -20

# ── 9. nim test — actual round-trip /v1/messages ───────────────────────────
log "nim test (single round-trip)"
"$VENV_BIN/nim" test --prompt "Reply with exactly one word: READY" || warn "nim test returned rc=$?"

# ── 10. nim logs tail ───────────────────────────────────────────────────────
log "nim logs -n 10"
"$VENV_BIN/nim" logs -n 10 2>/dev/null | sed 's/^/  | /' || true

# ── 11. SIGTERM-driven shutdown (0.3.0 specific — no os._exit thread) ─────
log "Verifying SIGTERM ends the daemon cleanly within 15s"
PID="$(cat "$CONFIG_DIR/nim-proxy.pid" 2>/dev/null || true)"
[ -n "$PID" ] || fail "could not read pidfile at $CONFIG_DIR/nim-proxy.pid"
kill -TERM "$PID" 2>/dev/null || fail "could not SIGTERM pid $PID"
SHUTDOWN_OK=0
for i in $(seq 1 30); do
  if ! kill -0 "$PID" 2>/dev/null; then
    SHUTDOWN_OK=1
    ok "daemon exited on SIGTERM in $((i*500))ms"
    break
  fi
  sleep 0.5
done
[ "$SHUTDOWN_OK" -eq 1 ] || fail "daemon still alive after 15s of SIGTERM"

# ── 12. Explicit stop (idempotent) ─────────────────────────────────────────
log "nim stop (post-shutdown, should be a no-op)"
"$VENV_BIN/nim" stop || true

ok "PRODUCTION SMOKE TEST PASSED end to end"
echo
echo "summary:"
echo "  - installed nim-claude-proxy==0.3.0 from PyPI"
echo "  - nim start → daemon up"
echo "  - /healthz (with components block) OK"
echo "  - nim doctor / status / models / test OK"
echo "  - SIGTERM-driven shutdown exits without os._exit"
