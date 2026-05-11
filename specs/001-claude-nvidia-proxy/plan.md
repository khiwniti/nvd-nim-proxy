# Implementation Plan: Claude NVIDIA Proxy

**Branch**: `001-claude-nvidia-proxy` | **Date**: 2026-05-12 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/001-claude-nvidia-proxy/spec.md`

## Summary

Deliver a local HTTP proxy that speaks the Anthropic Messages API to Claude Code on the
client side and the OpenAI-compatible chat completions API to NVIDIA NIM upstream. The
proxy translates requests, responses, streaming events, and tool-use round trips, maps
Claude model identifiers to NVIDIA model IDs through configurable aliases, and exposes
health and model listing endpoints. Technical approach follows the inspectable single-file
FastAPI translation proxy described in `Building a custom proxy for Claude Code.md`, with
secrets sourced from environment variables and non-secret routing from YAML.

## Technical Context

**Language/Version**: Python 3.11+
**Primary Dependencies**: FastAPI, uvicorn[standard], httpx[http2], orjson, PyYAML
**Storage**: N/A (stateless translation; in-memory tool-id map per request)
**Testing**: pytest (translation-layer unit tests; no live NVIDIA calls required)
**Target Platform**: Local developer workstation or controlled single-tenant host (macOS/Linux)
**Project Type**: Single-project CLI / web-service hybrid (single-file FastAPI app + tests)
**Performance Goals**: Emit `message_start` before first upstream token; non-streaming
overhead under 2s excluding upstream generation (SC-001, SC-002)
**Constraints**: Anthropic SSE event ordering preserved; unsupported Anthropic server tools
stripped rather than forwarded; secrets never logged or persisted to YAML
**Scale/Scope**: Single user / local deployment; one concurrent Claude Code session is the
primary target, with httpx async client allowing modest concurrency

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The project constitution at `.specify/memory/constitution.md` is the unfilled template
(placeholder principles `[PRINCIPLE_1_NAME]` … `[PRINCIPLE_5_NAME]`). No ratified
principles or gates are defined yet, so there are no constitution-imposed constraints to
evaluate. Result: **PASS by vacuous gate**. Re-evaluated after Phase 1 design with the
same result. If the constitution is later filled in, this plan should be re-checked.

## Project Structure

### Documentation (this feature)

```text
specs/001-claude-nvidia-proxy/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   └── anthropic-messages.md
├── checklists/
│   └── requirements.md
├── spec.md
└── tasks.md             # Phase 2 output (/speckit-tasks command)
```

### Source Code (repository root)

```text
proxy.py                 # Single-file FastAPI app: config loader, translation,
                         # SSE streaming, model aliasing, error mapping
config.example.yaml      # Non-secret routing config (host, port, base URL, aliases)
.env.example             # Secret env vars (NVIDIA_API_KEY, optional PROXY_API_KEY)
requirements.txt         # FastAPI, uvicorn, httpx, orjson, PyYAML, pytest
README.md                # User-facing docs: setup, Claude Code env vars, troubleshooting

tests/
├── test_translation.py  # Request/response/tool/error translation unit tests
└── test_streaming.py    # SSE event ordering and stream translation tests
```

**Structure Decision**: Single-project layout. The proxy is intentionally a single
inspectable file (`proxy.py`) at the repository root with sibling `tests/`,
configuration examples, and README. This matches the reference design in
`Building a custom proxy for Claude Code.md` and keeps the surface area small enough
that translation behavior — the core risk — is auditable in one place.

## Complexity Tracking

> No constitution violations to justify (constitution is unratified placeholder).
