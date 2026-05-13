# Tasks: Production-Ready CLI

**Input**: specs/002-production-cli/spec.md
**Status**: Complete

## Phase 1: Global Config & PID Management (Blocking)

- [x] T001 Add `config_dir()` helper returning `~/.config/nim-proxy/` (XDG, `APPDIR` override)
- [x] T002 Update config search order: global → local → env
- [x] T003 Add `pid_path()`, `log_path()` helpers pointing into config dir
- [x] T004 Add `is_running()` → reads PID file, checks process alive with `os.kill(pid, 0)`

## Phase 2: Daemon Lifecycle

- [x] T005 Implement `cmd_start(args)` — spawn proxy, write PID, wait for health (FR-004)
- [x] T006 Implement `cmd_stop(args)` — SIGTERM→wait→SIGKILL, remove PID file (FR-005)
- [x] T007 Implement `cmd_restart(args)` — stop + start (FR-007)
- [x] T008 Implement `cmd_logs(args)` — print last N lines; `-f` tail (FR-006)
- [x] T009 Wire `nim start`, `nim stop`, `nim restart`, `nim logs` subcommands into `main()`

## Phase 3: Enhanced Status & Init

- [x] T010 Update `cmd_status` to show PID from PID file, config path, daemon state
- [x] T011 Update `cmd_init` to write `~/.config/nim-proxy/config.yaml` (FR-010)
- [x] T012 `nim init` prints export lines for `ANTHROPIC_BASE_URL` and `ANTHROPIC_API_KEY`

## Phase 4: Doctor & Configure

- [x] T013 Implement `cmd_doctor(args)` — 6 checks, PASS/WARN/FAIL each (FR-009)
- [x] T014 Implement `cmd_configure(args)` — dot-key set + `--list` (FR-011, FR-012)
- [x] T015 Wire `nim doctor` and `nim configure` into `main()`

## Phase 5: `nim code` Daemon-Aware

- [x] T016 Update `cmd_code` — start daemon if not running; do NOT stop it when Claude exits

## Phase 6: Hardening

- [x] T017 Port-conflict detection: check if port busy before starting, print actionable message
- [x] T018 Update `nim version` to read version from `pyproject.toml` or package metadata
- [x] T019 Update README with full command reference and 2-minute quickstart
- [x] T020 Run all 16 existing tests, confirm still passing (16 passed)

## Dependencies

- T001-T004 must complete before T005-T009
- T005-T009 must complete before T016
- T010-T012 can run in parallel with T005-T009
- T013-T015 can run after T001-T004
