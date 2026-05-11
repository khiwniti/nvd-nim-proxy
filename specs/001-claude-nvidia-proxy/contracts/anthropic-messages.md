# Contract: Anthropic Messages Compatibility

## POST /v1/messages

Accepts Anthropic Messages API style JSON from Claude Code.

Required request fields:
- `model`: string
- `messages`: array

Optional request fields:
- `stream`: boolean
- `max_tokens`: integer
- `system`: string or text block array
- `tools`: Anthropic tool definitions
- `tool_choice`: Anthropic tool choice
- `metadata.user_id`: forwarded as upstream user
- `temperature`, `top_p`, `top_k`, `stop_sequences`

Non-streaming success response:
- Status: 200
- Body: Anthropic message object with `content` blocks and `usage`.

Streaming success response:
- Status: 200
- Content-Type: `text/event-stream`
- Event sequence includes:
  - `message_start`
  - zero or more `content_block_start`, `content_block_delta`, `content_block_stop`
  - `message_delta`
  - `message_stop`
  - optional `ping` during upstream silence

Error response:
- Status: upstream-like HTTP status where possible
- Body: `{ "type": "error", "error": { "type": "...", "message": "..." } }`

## POST /v1/messages/count_tokens

Accepts an Anthropic-style request body and returns a heuristic token count.

Success response:
- Status: 200
- Body: `{ "input_tokens": number }`

## GET /v1/models

Returns upstream NVIDIA model listing.

Success response:
- Status: upstream status
- Body: upstream model listing JSON

## GET /healthz

Success response:
- Status: 200
- Body: `{ "status": "ok" }`
