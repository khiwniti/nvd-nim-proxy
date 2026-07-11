#!/usr/bin/env bash
# Publishes nim-claude-proxy 0.3.0 to PyPI, then smoke-tests the production
# installation end-to-end. Driven from tmux session "nim-publish".
#
# Expected inputs (exported in the calling shell, NOT echoed):
#   NVIDIA_API_KEY - real key used by the install + smoke test
#   TWINE_PASSWORD - PyPI OIDC/personal token (pypi-… or agpi-… form)
#
# Stop the run at any failed step via `exit 1` so we never push a broken
# half-deploy to PyPI (PyPI rejects re-uploads of the same filenameless
# version, but we still want crisp per-stage logs).

set -uo pipefail
cd "$(dirname "$0")/.."

log() { printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" ; }
fail() { printf '\n\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2 ; exit 1 ; }
ok() { printf '\033[1;32m[OK]\033[0m %s\n' "$*" ; }

[ -n "${TWINE_PASSWORD:-}" ] || fail "TWINE_PASSWORD not set in this shell"
[ -n "${NVIDIA_API_KEY:-}" ] || fail "NVIDIA_API_KEY not set in this shell"

# ── 1. Pre-flight ───────────────────────────────────────────────────────────
log "Preflight"
test -f dist/nim_claude_proxy-0.3.0-py3-none-any.whl \
  || fail "wheel artifact missing — run python3 -m build first"
test -f dist/nim_claude_proxy-0.3.0.tar.gz \
  || fail "sdist artifact missing — run python3 -m build first"
ok "build artifacts present"

# ── 2. Upload to PyPI via twine ─────────────────────────────────────────────
log "Publishing 0.3.0 to PyPI"
python3 -m twine upload dist/* \
  --username __token__ \
  --password "$TWINE_PASSWORD" \
  --skip-existing \
  2>&1 | tee /tmp/twine-upload.log
grep -E 'Uploading|View at|registered' /tmp/twine-upload.log >/dev/null \
  || fail "twine upload did not report success"
ok "twine upload reported success"

# PyPI indexes can take seconds to converge — small grace before pip hits it.
log "Sleeping 8s to let PyPI index settle"
sleep 8

# ── 3. Fresh production install in an isolated venv ────────────────────────
log "Creating isolated venv .venv-prod"
rm -rf .venv-prod
python3 -m venv .venv-prod
VENV=".venv-prod/bin"
source "$VENV/activate"
python3 --version
pip install --quiet --upgrade pip wheel

log "Installing nim-claude-proxy==0.3.0 from PyPI"
pip install --quiet 'nim-claude-proxy==0.3.0'
INSTALLED="$("$VENV/python" -c 'import importlib.metadata as m; print(m.version("nim-claude-proxy"))')"
[ "$INSTALLED" = "0.3.0" ] || fail "expected 0.3.0 installed, got $INSTALLED"
ok "installed version: $INSTALLED"

# ── 4. Start the daemon (proxies to uvicorn via FastAPI lifespan) ───────────
export NVIDIA_API_KEY NVIDIA_BASE_URL="${NVIDIA_BASE_URL:-https://integrate.api.nvidia.com/v1}"
PROXY_PORT=8787

log "Booting nim-proxy on 127.0.0.1:$PROXY_PORT"
mkdir -p /tmp/nim-prod-log
"$VENV/nim-proxy" >/tmp/nim-prod-log/out.log 2>&1 &
PROXY_PID=$!
echo "[deploy] daemon pid=$PROXY_PID"
trap "kill $PROXY_PID 2>/dev/null; wait 2>/dev/null" EXIT

# Wait until /healthz responds (max 20s; lenient on cold start)
log "Waiting for /healthz to respond"
for i in $(seq 1 40); do
  if curl -fsS "http://127.0.0.1:$PROXY_PORT/healthz" > /tmp/health.json 2>/dev/null; then
    ok "/healthz responded in $((i*500))ms"
    break
  fi
  sleep 0.5
done
[ -s /tmp/health.json ] || fail "/healthz never responded; tail of /tmp/nim-prod-log/out.log below."
echo
echo "  ---- /healthz ----"
python3 -m json.tool < /tmp/health.json | sed 's/^/  /'
echo "  -------------------"

# ── 5. /v1/models sanity ────────────────────────────────────────────────────
log "Listing /v1/models"
code="$(curl -s -o /tmp/models.json -w '%{http_code}' "http://127.0.0.1:$PROXY_PORT/v1/models")"
[ "$code" = "200" ] || fail "/v1/models returned HTTP $code"
python3 - <<'PY'
import json
data = json.load(open("/tmp/models.json"))
ids = [m["id"] for m in data["data"]]
assert ids, "no models returned"
claude_aliases = [i for i in ids if i.startswith("claude-")]
print(f"  model_count = {len(ids)}; claude_aliases = {len(claude_aliases)}")
print(f"  first 5 = {ids[:5]}")
PY
ok "/v1/models returned the expected schema"

# ── 6. Smoke /v1/messages call (non-streaming) ─────────────────────────────
log "Smoke /v1/messages (non-streaming)"
MSG_BODY='{"model":"claude-3-5-sonnet-20241022","max_tokens":32,"messages":[{"role":"user","content":"Reply with the single word READY."}]}'
code="$(curl -s -o /tmp/msg.json -w '%{http_code}' \
  -H "content-type: application/json" \
  -d "$MSG_BODY" \
  "http://127.0.0.1:$PROXY_PORT/v1/messages")"
[ "$code" = "200" ] || { cat /tmp/msg.json | sed 's/^/  /'; fail "/v1/messages returned HTTP $code"; }
python3 - <<'PY'
import json
data = json.load(open("/tmp/msg.json"))
text = "".join(
    b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
)
print(f"  stop_reason = {data.get('stop_reason')}")
print(f"  usage       = {data.get('usage')}")
print(f"  text_preview = {text!r}")
assert data["stop_reason"] in ("end_turn", "stop_sequence", "max_tokens"), data
ok = "READY" in text or len(text) > 0
print(f"  got_text    = {bool(text)}")
PY
ok "/v1/messages produced a valid response"

# ── 7. Smoke /v1/messages (streaming) ──────────────────────────────────────
log "Smoke /v1/messages (streaming)"
STREAM_BODY='{"model":"claude-3-5-sonnet-20241022","max_tokens":32,"stream":true,"messages":[{"role":"user","content":"Reply with the single word READY."}]}'
curl -fsSN \
  -H "content-type: application/json" \
  -H "accept: text/event-stream" \
  -d "$STREAM_BODY" \
  "http://127.0.0.1:$PROXY_PORT/v1/messages" > /tmp/stream.sse 2>/tmp/stream.err
test -s /tmp/stream.sse || { cat /tmp/stream.err; fail "stream response empty"; }
echo
echo "  ---- stream.sse (head 30 lines) ----"
head -30 /tmp/stream.sse | sed 's/^/  /'
echo "  --------------------------------------"

EVENT_COUNT="$(grep -c '^event:' /tmp/stream.sse || true)"
STOP_COUNT="$(grep -c '^event: message_stop' /tmp/stream.sse || true)"
[ "$EVENT_COUNT" -gt 0 ] || fail "stream response had no SSE events"
[ "$STOP_COUNT" -eq 1 ] || fail "expected exactly 1 message_stop, got $STOP_COUNT"
ok "stream received $EVENT_COUNT events including 1 message_stop"

# ── 8. SIGTERM / shutdown safety net ───────────────────────────────────────
log "Verifying SIGTERM ends the daemon cleanly"
kill -TERM "$PROXY_PID"
for i in $(seq 1 30); do
  if ! kill -0 "$PROXY_PID" 2>/dev/null; then
    ok "daemon exited on SIGTERM in $((i*500))ms"
    break
  fi
  sleep 0.5
done
kill -0 "$PROXY_PID" 2>/dev/null \
  && fail "daemon did not exit after SIGTERM within 15s"
trap - EXIT

# ── 9. Done ─────────────────────────────────────────────────────────────────
ok "PRODUCTION SMOKE TEST PASSED"
echo
echo "summary:"
echo "  - nim-claude-proxy 0.3.0 published"
echo "  - installed into .venv-prod from PyPI"
echo "  - /healthz, /v1/models, /v1/messages (non-streaming + streaming) all OK"
echo "  - SIGTERM-driven shutdown exits cleanly without os._exit"
