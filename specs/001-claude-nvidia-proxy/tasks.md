# Tasks: Claude NVIDIA Proxy

**Input**: Design documents from `/specs/001-claude-nvidia-proxy/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: Tests are required for translation and streaming behavior.

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Establish spec artifacts and dependency baseline

- [x] T001 Create Spec Kit feature directory and specification in specs/001-claude-nvidia-proxy/spec.md
- [x] T002 Create implementation plan in specs/001-claude-nvidia-proxy/plan.md
- [x] T003 [P] Create research notes in specs/001-claude-nvidia-proxy/research.md
- [x] T004 [P] Create data model in specs/001-claude-nvidia-proxy/data-model.md
- [x] T005 [P] Create API contract in specs/001-claude-nvidia-proxy/contracts/anthropic-messages.md
- [x] T006 [P] Create quickstart in specs/001-claude-nvidia-proxy/quickstart.md

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core configuration and test structure that supports all stories

- [x] T007 Add YAML configuration loading and environment override support in proxy.py
- [x] T008 Add model alias resolution in proxy.py
- [x] T009 Add config.example.yaml with safe non-secret defaults
- [x] T010 Add pytest dependency entries in requirements.txt
- [x] T011 [P] Create tests directory and translation tests in tests/test_translation.py
- [x] T012 [P] Create stream translator tests in tests/test_streaming.py

**Checkpoint**: Foundation ready - user story implementation can now be validated

---

## Phase 3: User Story 1 - Use NVIDIA models from Claude Code (Priority: P1) 🎯 MVP

**Goal**: Claude Code can send Anthropic Messages requests and receive Anthropic-compatible responses backed by NVIDIA.

**Independent Test**: Run pytest translation tests and the quickstart curl examples.

### Tests for User Story 1

- [x] T013 [P] [US1] Test Claude model alias to NVIDIA model mapping in tests/test_translation.py
- [x] T014 [P] [US1] Test non-streaming response translation in tests/test_translation.py

### Implementation for User Story 1

- [x] T015 [US1] Apply resolved upstream model in translate_request within proxy.py
- [x] T016 [US1] Update README.md quickstart to mention config.yaml model aliasing

**Checkpoint**: User Story 1 should work independently

---

## Phase 4: User Story 2 - Run tool-using Claude Code workflows (Priority: P2)

**Goal**: Tool definitions, tool calls, and tool results round-trip safely.

**Independent Test**: Unit tests verify Anthropic tool blocks become OpenAI function tools and back.

### Tests for User Story 2

- [x] T017 [P] [US2] Test Anthropic tool definition translation in tests/test_translation.py
- [x] T018 [P] [US2] Test OpenAI tool call to Anthropic tool_use translation in tests/test_translation.py

### Implementation for User Story 2

- [x] T019 [US2] Verify unsupported Anthropic server tools are filtered in proxy.py
- [x] T020 [US2] Document server-tool limitations in README.md

**Checkpoint**: Tool workflow translation should be test-covered

---

## Phase 5: User Story 3 - Operate and troubleshoot reliably (Priority: P3)

**Goal**: Users can configure, test, and diagnose the proxy reliably.

**Independent Test**: Run pytest, inspect example config, and check health endpoint.

### Tests for User Story 3

- [x] T021 [P] [US3] Test error type mapping in tests/test_translation.py
- [x] T022 [P] [US3] Test stream translator event order in tests/test_streaming.py

### Implementation for User Story 3

- [x] T023 [US3] Add config details and troubleshooting notes to README.md
- [x] T024 [US3] Run pytest and fix failures

**Checkpoint**: Operations and diagnostics are documented and tested

---

## Final Phase: Polish & Cross-Cutting Concerns

- [x] T025 Update CLAUDE.md plan pointer to specs/001-claude-nvidia-proxy/plan.md
- [x] T026 Run syntax check for proxy.py
- [x] T027 Run git status and summarize changed files

## Post-Analyze Remediation (2026-05-12)

- [x] T028 Add route-level smoke tests for /healthz and /v1/messages validation in tests/test_routes.py (FR-007, FR-001 coverage)
- [x] T029 Add tests/conftest.py to stub NVIDIA_API_KEY so route tests run offline
- [x] T030 Document env-overrides-YAML precedence in research.md (resolves U2)
- [x] T031 Add eager message_start streaming test in tests/test_stream_eager.py (resolves C3, SC-002)
- [x] T032 Pin pytest-asyncio in requirements.txt

## Dependencies & Execution Order

- Setup tasks T001-T006 are complete.
- Foundational tasks T007-T012 block story validation.
- US1 is MVP and should complete before README finalization.
- US2 and US3 tests can run in parallel after foundational setup.

## Parallel Opportunities

- T011 and T012 can be authored in parallel.
- T013, T014, T017, T018, T021, and T022 are independent test cases.
- Documentation updates can proceed after implementation details stabilize.

## Implementation Strategy

1. Add configuration and aliasing first.
2. Add tests for existing and new behavior.
3. Update docs and quickstart.
4. Run full validation.

