# Feature Specification: Production-Ready CLI

**Feature Branch**: `002-production-cli`
**Created**: 2026-05-13
**Status**: Active
**Input**: "Make this production ready with fully user flow end to end (like Claude Code Router / OpenClaude)"

## Goal

Elevate `nim` from a developer script into a production-grade CLI that any user can install, configure, and operate without reading source code — matching the UX standard set by Claude Code Router and OpenClaude.

## User Stories

### US1 — First-time user gets running in < 2 minutes (P1)

A developer installs the package, runs `nim init`, and is guided through API key entry, model selection, and proxy startup. They see the exact Claude Code environment variables to set. No file editing required.

**Acceptance criteria:**
1. WHEN user runs `nim init` THEN wizard prompts for NVIDIA_API_KEY, shows available models, saves to `~/.config/nim-proxy/config.yaml`, and prints the ANTHROPIC_BASE_URL to export.
2. WHEN user runs `nim start` THEN proxy starts as a background daemon and returns the shell prompt.
3. WHEN user runs `nim status` THEN output shows: running/stopped, PID, URL, active model, key configured.

### US2 — Daemon lifecycle: start / stop / restart / logs (P1)

A developer can start the proxy once and leave it running across Claude Code sessions, stopping it explicitly when done.

**Acceptance criteria:**
1. WHEN user runs `nim start` AND proxy is already running THEN prints "already running" with PID and exits 0.
2. WHEN user runs `nim stop` THEN daemon is terminated gracefully (SIGTERM, then SIGKILL after 5s) and PID file removed.
3. WHEN user runs `nim logs` THEN last 50 lines of proxy log are printed; `-f` flag tails live.
4. WHEN user runs `nim restart` THEN stop + start sequence completes.

### US3 — `nim doctor` diagnoses configuration problems (P2)

A developer can run one command to understand why the proxy is not working.

**Acceptance criteria:**
1. WHEN user runs `nim doctor` THEN each check shows PASS/WARN/FAIL with a one-line explanation:
   - Python version (≥ 3.9)
   - NVIDIA_API_KEY present and non-empty
   - NVIDIA API reachable (HEAD to base URL)
   - Proxy port free or proxy running
   - Claude Code installed (`claude --version`)
   - Config file location and contents summary

### US4 — `nim configure` sets config values without editing files (P2)

**Acceptance criteria:**
1. WHEN user runs `nim configure key value` THEN value is written to `~/.config/nim-proxy/config.yaml`.
2. WHEN user runs `nim configure --list` THEN current effective config is printed.

### US5 — `nim code` launches Claude Code with correct env (P1)

A developer runs `nim code` and Claude Code opens pointed at the proxy with the right model, without manually exporting environment variables.

**Acceptance criteria:**
1. WHEN proxy is not running AND user runs `nim code` THEN proxy starts as daemon first, then Claude Code launches.
2. WHEN proxy is running AND user runs `nim code` THEN Claude Code launches immediately using existing daemon.
3. WHEN Claude Code exits THEN proxy keeps running (daemon lifecycle is separate).

## Requirements

### Functional

- **FR-001**: Global config directory at `~/.config/nim-proxy/` (XDG Base Dir compliant, `APPDIR` override).
- **FR-002**: Config search order: `~/.config/nim-proxy/config.yaml` → `./config.yaml` → env vars.
- **FR-003**: PID file at `~/.config/nim-proxy/nim-proxy.pid`; logs at `~/.config/nim-proxy/nim-proxy.log`.
- **FR-004**: `nim start` starts proxy as background process, writes PID file, waits up to 10s for health.
- **FR-005**: `nim stop` reads PID file, sends SIGTERM, waits 5s, falls back to SIGKILL, removes PID file.
- **FR-006**: `nim logs [-f] [-n N]` reads log file; `-f` tails with `follow=True`.
- **FR-007**: `nim restart` = stop + start.
- **FR-008**: `nim status` shows running/stopped, PID, URL, model, key status.
- **FR-009**: `nim doctor` runs all diagnostic checks and prints PASS/WARN/FAIL per item.
- **FR-010**: `nim init` saves to global config dir; never writes `.env` in CWD unless `--local` flag set.
- **FR-011**: `nim configure key value` updates `~/.config/nim-proxy/config.yaml` dot-notation key.
- **FR-012**: `nim configure --list` prints effective config (secrets redacted).
- **FR-013**: `nim code` starts daemon if needed, then execs `claude` — proxy keeps running after Claude exits.
- **FR-014**: Port-already-in-use is detected and reported with a clear message and suggested fix.

### Non-Functional

- **NFR-001**: No new dependencies beyond what is already in `requirements.txt` (`pathlib`, `signal`, `subprocess` are stdlib).
- **NFR-002**: All commands complete their own I/O within 500ms excluding network operations.
- **NFR-003**: Log file is append-only; rotated at 5 MB to `nim-proxy.log.1`.
- **NFR-004**: Secrets (API key) are never printed in `--list` output (shown as `****`).

## Success Criteria

- **SC-001**: A user with only `pip install nvd-claude-nim` and a NVIDIA API key can be running Claude Code in under 2 minutes.
- **SC-002**: `nim start` / `nim stop` round-trip works reliably with no zombie processes.
- **SC-003**: `nim doctor` correctly identifies a missing API key, unreachable proxy, and missing Claude Code.
- **SC-004**: All existing 16 tests still pass after changes.
