# Feature Specification: Claude NVIDIA Proxy

**Feature Branch**: `001-claude-nvidia-proxy`  
**Created**: 2026-05-11  
**Status**: Draft  
**Input**: User description: "Create a comprehensive plan and build Claude Code proxy integrating with NVIDIA API aligned on Building a custom proxy for Claude Code.md"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Use NVIDIA models from Claude Code (Priority: P1)

A developer can point Claude Code at a local proxy and run a coding session backed by NVIDIA-hosted NIM models while Claude Code continues to speak the Anthropic Messages API.

**Why this priority**: This is the core user value; without it the proxy does not solve the integration problem.

**Independent Test**: Start the proxy with an NVIDIA key, send a `/v1/messages` request in Anthropic format, and receive an Anthropic-format assistant message backed by NVIDIA.

**Acceptance Scenarios**:

1. **Given** the proxy is running and configured with an NVIDIA API key, **When** Claude Code sends a non-streaming Anthropic messages request, **Then** the proxy returns a valid Anthropic message response.
2. **Given** the proxy is running and Claude Code requests streaming, **When** NVIDIA emits OpenAI-style streaming chunks, **Then** the proxy emits Anthropic-compatible SSE events in the expected order.
3. **Given** Claude Code requests a built-in Claude model name, **When** model alias mapping is configured, **Then** the proxy routes the request to the configured NVIDIA model.

---

### User Story 2 - Run tool-using Claude Code workflows (Priority: P2)

A developer can use Claude Code workflows that invoke client-side tools, with the proxy translating tool definitions, tool calls, and tool results between Anthropic and OpenAI-compatible formats.

**Why this priority**: Claude Code relies heavily on tool use for coding tasks; chat-only behavior is insufficient for real use.

**Independent Test**: Send a request containing Anthropic `tools`, verify the upstream payload contains OpenAI `tool_calls` schema, then verify an OpenAI tool call response is converted back to Anthropic `tool_use` blocks.

**Acceptance Scenarios**:

1. **Given** Claude Code sends tool definitions, **When** the proxy forwards the request, **Then** supported client-side tools are converted to OpenAI function tools.
2. **Given** NVIDIA returns a tool call, **When** the proxy translates the response, **Then** Claude Code receives a valid `tool_use` block.
3. **Given** Claude Code sends `tool_result` blocks, **When** the proxy forwards the next turn, **Then** the upstream receives matching OpenAI `tool` messages.

---

### User Story 3 - Operate and troubleshoot reliably (Priority: P3)

A developer can configure, validate, and troubleshoot the proxy with clear docs, health checks, predictable errors, and tests covering translation behavior.

**Why this priority**: The proxy must be usable beyond a one-off script, especially when integrating with a CLI that streams and retries.

**Independent Test**: Run automated tests, check `/healthz`, inspect example configuration, and reproduce quickstart requests without Claude Code.

**Acceptance Scenarios**:

1. **Given** the proxy starts, **When** a health check is requested, **Then** it returns an OK status.
2. **Given** NVIDIA returns an error, **When** the proxy relays it, **Then** the response uses Anthropic-style error type and clear message.
3. **Given** the user wants to change models or ports, **When** they edit the configuration file or environment variables, **Then** the proxy uses those settings without code changes.

### Edge Cases

- NVIDIA API key is missing or invalid.
- Claude Code sends beta headers or server-side Anthropic tool definitions that NVIDIA cannot support.
- Streaming upstream returns an error after the downstream SSE response has already started.
- NVIDIA emits coarse chunks, reasoning content, inline `<think>` tags, malformed JSON tool arguments, or usage-only chunks.
- Claude Code requests a Claude model alias not present in NVIDIA's model catalog.
- Client disconnects while the proxy is still waiting on upstream streaming.
- NVIDIA rate limits the account.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST expose an Anthropic-compatible `/v1/messages` endpoint for streaming and non-streaming requests.
- **FR-002**: System MUST translate Anthropic request messages, system prompts, image blocks, tools, tool choice, and tool results into NVIDIA-compatible chat completion requests.
- **FR-003**: System MUST translate NVIDIA chat completion responses into Anthropic message responses including text, thinking, tool use, stop reasons, and usage fields.
- **FR-004**: System MUST emit Anthropic-compatible SSE events for streaming responses, including eager message start, content block lifecycle events, deltas, final message delta, and message stop.
- **FR-005**: System MUST map configured Claude model names to configured NVIDIA model names before forwarding upstream.
- **FR-006**: System MUST support configuration through environment variables and a YAML file.
- **FR-007**: System MUST provide a health endpoint and model listing endpoint.
- **FR-008**: System MUST surface upstream authentication, rate limit, invalid request, not found, overload, and generic API errors in Anthropic-style error envelopes.
- **FR-009**: System MUST ignore or strip unsupported Anthropic-only server tools and cache markers rather than forwarding incompatible payload fields upstream.
- **FR-010**: System MUST include documentation explaining Claude Code environment variables, model selection, limitations, troubleshooting, and direct curl tests.
- **FR-011**: System MUST include automated tests covering request translation, response translation, stream translation, model aliasing, and error mapping.

### Key Entities

- **Proxy Configuration**: Host, port, upstream base URL, NVIDIA key source, optional proxy key, log level, default model, model alias map, streaming settings.
- **Anthropic Request**: Client request shape from Claude Code including model, system, messages, tools, tool choice, max tokens, metadata, and stream flag.
- **NVIDIA Request**: OpenAI-compatible chat completion payload sent upstream.
- **Anthropic Response**: Message or SSE event sequence returned to Claude Code.
- **Model Alias**: Mapping from a Claude Code requested model name to an NVIDIA model identifier.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A valid non-streaming `/v1/messages` request returns a valid Anthropic response in under 2 seconds excluding upstream model generation time.
- **SC-002**: A streaming request emits `message_start` before waiting for the first upstream token.
- **SC-003**: At least 90% of common Claude Code request fields used in local coding workflows are either translated correctly or explicitly ignored with safe behavior.
- **SC-004**: Automated tests cover the critical translation paths and pass locally with one command.
- **SC-005**: A new user can configure the proxy and run a documented curl request within 10 minutes.

## Assumptions

- Hosted NVIDIA NIM uses an OpenAI-compatible chat completions API at `https://integrate.api.nvidia.com/v1`.
- Claude Code client-side tools are usable when represented as function tools; Anthropic-managed server tools have no NVIDIA-hosted equivalent and are out of scope.
- YAML configuration supplements environment variables; secrets should usually remain in environment variables rather than committed config files.
- The proxy is intended for local or controlled deployment rather than a public multi-tenant gateway.
