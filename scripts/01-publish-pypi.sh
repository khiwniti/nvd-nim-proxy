#!/usr/bin/env bash
# Stage 1: Publish nim-claude-proxy 0.3.0 to PyPI via twine.
#
# Run from the tmux session "nim-publish" (or any shell) with the env
# variables below already exported. This script does NOT take flags —
# any failure aborts the upload (PyPI rejects re-uploads of the same
# filename-version, so we want crisp per-stage logs).

set -uo pipefail
cd "$(dirname "$0")/.."

log() { printf '\n\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*" ; }
fail() { printf '\n\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2 ; exit 1 ; }
ok() { printf '\033[1;32m[OK]\033[0m %s\n' "$*" ; }

[ -n "${TWINE_PASSWORD:-}" ] || fail "TWINE_PASSWORD not set in this shell"

log "Preflight"
test -f dist/nim_claude_proxy-0.3.0-py3-none-any.whl \
  || fail "wheel artifact missing — run python3 -m build first"
test -f dist/nim_claude_proxy-0.3.0.tar.gz \
  || fail "sdist artifact missing — run python3 -m build first"
ok "build artifacts present"

log "twine check (defensive)"
python3 -m twine check dist/* | tee /tmp/twine-check.log
grep -qE 'PASSED' /tmp/twine-check.log || fail "twine check did not pass"
ok "twine check PASSED"

log "Publishing 0.3.0 to PyPI"
python3 -m twine upload dist/* \
  --username __token__ \
  --password "$TWINE_PASSWORD" \
  --skip-existing \
  2>&1 | tee /tmp/twine-upload.log
grep -qE 'View at:|Uploading.*to https://upload\.pypi\.org' /tmp/twine-upload.log \
  || fail "twine upload did not report success"
ok "twine upload reported success"

log "Sleeping 6s for PyPI index to converge"
sleep 6

log "Confirming 0.3.0 is on PyPI"
PYPI_JSON="$(curl -fsSL 'https://pypi.org/pypi/nim-claude-proxy/json')"
PYPI_VER="$(python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d['info']['version'])" <<<"$PYPI_JSON")"
[ "$PYPI_VER" = "0.3.0" ] || fail "PyPI reports latest version $PYPI_VER, expected 0.3.0"
ok "PyPI shows the latest release as $PYPI_VER"

ok "PUBLISHED nim-claude-proxy==0.3.0 to PyPI"
