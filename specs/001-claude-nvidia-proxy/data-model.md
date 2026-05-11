# Data Model: Claude NVIDIA Proxy

## ProxyConfig

Represents runtime configuration.

Fields:
- `host`: bind host for the local server
- `port`: bind port for the local server
- `nvidia_base_url`: upstream NVIDIA API base URL
- `nvidia_api_key`: upstream bearer token, normally sourced from environment
- `proxy_api_key`: optional client-facing key accepted via `x-api-key` or bearer auth
- `log_level`: server log verbosity
- `default_model`: fallback NVIDIA model for unmapped Claude model names
- `model_aliases`: mapping of client-requested model names to upstream NVIDIA model names
- `ping_interval`: SSE heartbeat interval during upstream silence
- `text_delta_chars`: soft re-tokenization chunk size for Anthropic-like streaming feel

Validation:
- `port` must be a valid TCP port.
- `nvidia_api_key` must be present when starting the server.
- Model alias keys and values must be non-empty strings.

## AnthropicMessageRequest

Represents inbound Claude Code request.

Fields:
- `model`
- `max_tokens`
- `stream`
- `system`
- `messages`
- `tools`
- `tool_choice`
- `metadata`
- sampling controls such as `temperature`, `top_p`, `top_k`, and `stop_sequences`

Validation:
- `model` and `messages` are required.
- Unknown Anthropic-specific fields are ignored unless directly translatable.

## OpenAIChatCompletionRequest

Represents upstream NVIDIA request.

Fields:
- `model`
- `messages`
- `max_tokens`
- `stream`
- optional `tools`, `tool_choice`, `user`, sampling controls, and stream usage options

Validation:
- `model` is resolved through alias mapping before upstream forwarding.
- Unsupported server-side Anthropic tools are omitted.

## ToolIdMap

Maintains round-trip association between Anthropic `toolu_*` identifiers and OpenAI tool call identifiers.

Fields:
- map from Anthropic tool ID to OpenAI tool call ID

Validation:
- Preserve Anthropic IDs when possible.
- Generate Anthropic-like IDs when upstream IDs do not match expected shape.

## AnthropicResponse

Represents outbound response to Claude Code.

Fields:
- `id`, `type`, `role`, `model`, `content`, `stop_reason`, `stop_sequence`, `usage`
- Content blocks may include `text`, `thinking`, or `tool_use`.

Validation:
- Streaming responses must maintain content block start/delta/stop order.
- Usage fields should always exist even if upstream omits them.
