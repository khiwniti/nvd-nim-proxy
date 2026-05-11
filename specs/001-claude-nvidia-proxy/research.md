# Research: Claude NVIDIA Proxy

## Decision: Use a custom FastAPI translation proxy instead of LiteLLM

**Rationale**: The repository already contains an inspectable single-file implementation with Claude Code-specific behavior: eager SSE start, ping heartbeat, reasoning block handling, server-tool filtering, and tool-use round-trip handling. A custom proxy avoids LiteLLM configuration ambiguity and allows precise compatibility fixes for Claude Code.

**Alternatives considered**:
- LiteLLM proxy: mature gateway and a known compatible baseline, but less transparent for Claude Code-specific SSE/tool edge cases.
- Direct NVIDIA `/v1/messages`: suitable for self-hosted NIM when available, but hosted catalog compatibility requires OpenAI chat completions translation.

## Decision: Load secrets from environment and non-secret routing from YAML

**Rationale**: NVIDIA keys should not be committed. YAML is useful for host/port/base URL/model aliases, while env vars remain safer for secrets and deployment overrides.

**Precedence**: Environment variables override YAML. Model aliases merge (YAML extends defaults; `MODEL_ALIAS_*` env vars then override individual entries).

**Alternatives considered**:
- Environment only: simple, but cumbersome for model alias maps.
- YAML only: easier local config, but risks secret leakage.

## Decision: Map Claude Code model names to NVIDIA model IDs inside the proxy

**Rationale**: Claude Code may request built-in Claude model identifiers even when pointed at a custom base URL. Mapping these to NVIDIA model IDs avoids upstream 404s and makes `/model`/fallback behavior more robust.

**Alternatives considered**:
- Require users to set all Claude Code model env vars: works but brittle and easy to forget.
- Fail fast on unmapped models: clear but less ergonomic.

## Decision: Preserve streaming responsiveness with eager `message_start` and ping events

**Rationale**: Claude Code is streaming-first. Some NVIDIA reasoning models have delayed first token; eager start and heartbeat prevent the UI from appearing frozen.

**Alternatives considered**:
- Wait for upstream first chunk: simpler but poor UX during long reasoning phases.

## Decision: Use pytest for translation-layer tests

**Rationale**: Most critical behavior is pure transformation logic. Fast unit tests catch regressions without requiring live NVIDIA credentials.

**Alternatives considered**:
- Live integration tests only: more realistic but slower, flaky, and key-dependent.
