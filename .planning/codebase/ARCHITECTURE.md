# Architecture

**Analysis Date:** 2026-05-13

## System Overview

```text
┌──────────────────────────────────────────────────────────────┐
│                    Claude Code (client)                       │
│   ANTHROPIC_BASE_URL=http://127.0.0.1:8787                   │
└────────────────────────┬─────────────────────────────────────┘
                         │  Anthropic Messages API
                         │  POST /v1/messages
                         ▼
┌──────────────────────────────────────────────────────────────┐
│              nvd-claude-proxy  (proxy.py)                     │
│                                                              │
│  FastAPI + Uvicorn                                           │
│  ┌────────────┐  ┌──────────────┐  ┌─────────────────────┐  │
│  │ Auth check │→ │  translate_  │→ │  stream_response()  │  │
│  │ check_auth │  │  request()   │  │  or translate_      │  │
│  └────────────┘  └──────────────┘  │  response()         │  │
│                                    └─────────────────────┘  │
│                         │                    │               │
│              translate_messages()     StreamTranslator        │
│              translate_tools()        .feed() / .finalize()   │
│              resolve_model()                                  │
└────────────────────────┬─────────────────────────────────────┘
                         │  OpenAI Chat Completions API
                         │  POST /chat/completions
                         ▼
┌──────────────────────────────────────────────────────────────┐
│            NVIDIA NIM  (integrate.api.nvidia.com/v1)         │
│   nvidia/llama-3.3-nemotron-super-49b-v1.5 (default)         │
└──────────────────────────────────────────────────────────────┘
```

## Key Components

| Component | File | Responsibility |
|-----------|------|----------------|
| FastAPI application | `proxy.py` (line 158) | HTTP server, route dispatch, lifespan |
| Config loader | `proxy.py` (`_load_yaml_config`, line 67) | Merges YAML config with env vars; env vars win |
| Model resolver | `proxy.py` (`resolve_model`, line 118) | Maps Claude model names to NVIDIA model IDs |
| Request translator | `proxy.py` (`translate_request`, line 359) | Anthropic body → OpenAI `/chat/completions` payload |
| Message translator | `proxy.py` (`translate_messages`, line 224) | Per-message block conversion including tool_result explosion |
| Tool translator | `proxy.py` (`translate_tools`, line 307) | Anthropic tool schema → OpenAI function schema; drops server-side tools |
| Response translator | `proxy.py` (`translate_response`, line 426) | One-shot OpenAI response → Anthropic Message |
| StreamTranslator | `proxy.py` (`StreamTranslator`, line 513) | Stateful OpenAI SSE → Anthropic SSE event machine |
| stream_response | `proxy.py` (`stream_response`, line 862) | Async generator: eager message_start, producer/consumer queue, ping loop |
| httpx client | `proxy.py` (lifespan, line 132) | Single shared AsyncClient with HTTP/2, pooling, 600 s read timeout |
| nim_code CLI | `nim_code.py` | Developer tool: start proxy+Claude, status, models, test, init subcommands |

## Data Flow

### Non-Streaming Request

```
Claude Code
  POST /v1/messages  (Anthropic JSON body)
    │
    ├── check_auth()              # optional PROXY_API_KEY validation
    ├── translate_request()
    │     ├── resolve_model()     # "claude-3-5-sonnet-20241022" → "nvidia/llama-3.3-nemotron-super-49b-v1.5"
    │     ├── flatten_system()    # system: [blocks] → plain string
    │     ├── inject tool protocol system prompt (if tools present)
    │     ├── translate_messages()
    │     │     ├── text blocks      → {"role":..., "content": str}
    │     │     ├── image blocks     → {"type":"image_url", "image_url": {...}}
    │     │     ├── tool_use blocks  → assistant msg with {"tool_calls": [...]}
    │     │     └── tool_result      → explodes to user msg + role:"tool" msg
    │     └── translate_tools()   # Anthropic schema → OpenAI function schema
    │                             # drops SERVER_TOOL_RE matches (web_search_20250305, etc.)
    │
    ├── nvidia.post("/chat/completions", json=payload)
    │
    └── translate_response()
          ├── extract_thinking()  # <think>…</think> or reasoning_content field
          ├── thinking block      → {"type":"thinking", "signature": "proxy-..."}
          ├── text block          → {"type":"text", "text": "..."}
          ├── tool_calls          → {"type":"tool_use", "id": "toolu_...", ...}
          └── usage mapping       prompt_tokens → input_tokens
```

### Streaming Request

```
Claude Code
  POST /v1/messages  (stream: true)
    │
    ├── [same translation as non-streaming up to translate_request()]
    │
    └── StreamingResponse(stream_response())
          │
          ├── yield message_start SSE immediately (before upstream connect)
          │
          ├── asyncio.create_task(producer())
          │     └── nvidia.stream("POST", "/chat/completions")
          │           └── per SSE line → json.loads() → queue.put(parsed_chunk)
          │
          └── consumer loop (asyncio.wait_for, PING_INTERVAL=15s timeout)
                ├── timeout → yield SSE "ping" event, check is_disconnected()
                ├── DONE sentinel → break
                ├── ERROR sentinel → yield SSE "error" event, break
                └── chunk → StreamTranslator.feed(chunk)
                      ├── reasoning_content delta → _emit_thinking()
                      ├── content delta → _process_text()
                      │     └── handles inline <think> tags across chunk boundaries
                      └── tool_call delta → content_block_start / input_json_delta
                finally: StreamTranslator.finalize()
                      └── message_delta (stop_reason, usage) + message_stop
```

## Request Lifecycle

### Streaming Content Block State Machine (`StreamTranslator`)

`StreamTranslator` (`proxy.py`, line 513) tracks one open content block at a time. All block transitions close the previous block before opening a new one:

```
None ──────────────→ open_text()      → content_block_start (type=text)
                  → open_thinking()  → content_block_start (type=thinking)
                  → tool start       → content_block_start (type=tool_use)

Any open block → _close_open() → content_block_stop
  (emitted before any different block type opens)
```

Key state fields on `StreamTranslator`:
- `open_type` (`str | None`): `"text"`, `"thinking"`, or `"tool_use"`
- `open_index` (`int | None`): content block index for the currently open block
- `in_inline_think` (`bool`): whether currently inside a `<think>…</think>` span
- `text_buf` (`str`): partial text buffered when a tag may straddle a chunk boundary
- `tools` (`dict[int, dict]`): per OpenAI tool-call-index state (id, name, started flag)

### Thinking Block Handling

Two input surfaces are supported transparently:

1. **`reasoning_content` field** in the delta (Nemotron's native reasoning output): forwarded directly to `_emit_thinking()`.
2. **Inline `<think>…</think>` tags** embedded in the `content` delta: parsed by `_process_text()`, which uses `_safe_suffix_len()` to detect partial tag prefixes at chunk boundaries and hold them in `text_buf` rather than emitting prematurely.

### Tool ID Mapping

A `tool_id_map: dict[str, str]` dict is created per-request and threaded through all translation functions:

- Anthropic uses `toolu_`-prefixed IDs; OpenAI uses `call_`-prefixed IDs.
- On request translation: `tool_id_map[anth_id] = anth_id` (Anthropic IDs passed verbatim as OpenAI call IDs).
- On response/stream translation: if OpenAI returns a non-`toolu_` ID, a new `toolu_` ID is generated and the mapping recorded so future tool_result messages can resolve correctly.

### Model Resolution Order (`resolve_model`, line 118)

1. Exact match in `MODEL_ALIASES` dict → return mapped NVIDIA model ID
2. Starts with `"claude-"` or `"anthropic/claude-"` → return `DEFAULT_MODEL`
3. Otherwise → pass through unchanged (native NVIDIA model IDs flow through directly)

## Key Design Decisions

### Single-File Architecture
`proxy.py` is intentionally self-contained (~1000 lines). There is no service layer, schema library, or ORM. Config, translation, streaming, and routing all live in one importable module. This makes test imports (`import proxy`) trivial and deployment a single `python proxy.py`.

### Eager `message_start`
The streaming path yields `message_start` as the very first `yield` statement (`proxy.py`, line 875), before the `async with nvidia.stream(...)` context manager is entered. This ensures Claude Code's TUI receives a response token immediately, even when Nemotron Ultra takes 30+ seconds to begin reasoning output.

### Producer/Consumer Queue with Ping Loop
Streaming uses `asyncio.Queue(maxsize=256)` with a dedicated `producer` asyncio task. The consumer uses `asyncio.wait_for(queue.get(), timeout=PING_INTERVAL)` (default 15 s). On timeout, an Anthropic `ping` SSE event is emitted — matching Anthropic's official keep-alive cadence — and client disconnect is checked.

### Text Retokenization
NVIDIA NIM returns 10–40 character chunks. `retokenize()` (`proxy.py`, line 482) re-splits at word/punctuation boundaries into pieces of at most `TEXT_DELTA_CHARS` characters (default 6, configurable) to recreate Anthropic's sub-word typing feel in the Claude Code TUI.

### Tool Description Capping
`translate_tools()` aggressively caps tool descriptions based on tool count to prevent NVIDIA context window overflow:
- ≤40 tools: 480 char cap
- 41–100 tools: 280 char cap
- >100 tools: 160 char cap

### Config Layering (module load time)
Priority (highest wins):
1. Environment variables (e.g., `NVIDIA_API_KEY`, `PROXY_PORT`)
2. `config.yaml` values (path overridable via `PROXY_CONFIG` env var)
3. Hardcoded defaults in `proxy.py`

`MODEL_ALIAS_*` env vars are also supported for per-alias overrides (e.g., `MODEL_ALIAS_CLAUDE_HAIKU_4_5=nvidia/some-model`).

### Server-Side Tool Filtering
Anthropic server-side tools (e.g., `web_search_20250305`, `computer_20250124`, `bash_20250124`) are identified by `SERVER_TOOL_RE = re.compile(r"_20\d{6}$")` and silently dropped — NVIDIA NIM has no equivalents.

### Tool Protocol System Prompt Injection
When any tools are present, `_TOOL_PROTOCOL_SYSTEM_PROMPT` is prepended to the system message. This prevents Nemotron/Llama models from hallucinating XML-style tags instead of using the native `tool_calls` JSON API.

### Heuristic Token Counting
`POST /v1/messages/count_tokens` uses `len(all_string_chars) // 4` (±15% accuracy for Llama/Nemotron/Qwen). NVIDIA hosted catalog has no native tokenization endpoint.

### Optional Performance Packages
`uvloop` (faster event loop) and `httptools` (faster HTTP parser) are enabled opportunistically if installed, with silent fallback to asyncio defaults.

## Constraints & Trade-offs

- **Thinking block signatures are opaque**: `new_signature()` returns `proxy-<random>` strings that will not validate against the real Anthropic API. Round-tripping thinking blocks to Anthropic is unsupported.
- **`max_tokens` clamped at 16384**: requests above this are silently reduced to prevent NVIDIA NIM token-limit errors.
- **No retry logic**: failed NVIDIA requests propagate as error SSE events or 502 responses immediately.
- **No persistence**: the proxy is fully stateless; all conversation state lives in the client.
- **`document` and `redacted_thinking` content blocks are silently dropped** in `translate_messages()`.
- **HTTP/2 is optional**: enabled only if the `h2` package is installed; falls back to HTTP/1.1 transparently.
- **600 s max stream duration**: set by the httpx `read` timeout; very long Nemotron Ultra reasoning chains approach this limit.

---

*Architecture analysis: 2026-05-13*
