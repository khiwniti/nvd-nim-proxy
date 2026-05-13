# Integrations

**Analysis Date:** 2026-05-13

## External APIs

**NVIDIA NIM Hosted Catalog (upstream):**
- Purpose: The sole upstream LLM inference provider; all Claude API requests are translated and forwarded here
- Base URL: `https://integrate.api.nvidia.com/v1` (configurable via `NVIDIA_BASE_URL`)
- API standard: OpenAI-compatible Chat Completions (`/v1/chat/completions`, `/v1/models`)
- Endpoints consumed:
  - `POST /chat/completions` — non-streaming and streaming inference (`proxy.py` lines 829, 907)
  - `GET /models` — proxied verbatim to clients (`proxy.py` line 781)
- Auth: Bearer token (`Authorization: Bearer <NVIDIA_API_KEY>`)
- Transport: HTTP/2 when `h2` package is present; falls back to HTTP/1.1
- Client: shared `httpx.AsyncClient` created in FastAPI lifespan (`proxy.py` lines 140–155)
- Streaming: SSE (`text/event-stream`), `[DONE]` sentinel, `stream_options: {include_usage: true}`
- Timeout: connect=10s, read=600s (accommodates 30+ second reasoning phases)

**Anthropic Claude Code (client-side):**
- Role: This proxy impersonates the Anthropic Messages API so Claude Code can use it without modification
- Protocol: Anthropic Messages API v1 (`/v1/messages`, `/v1/messages/count_tokens`, `/v1/models`)
- Client points `ANTHROPIC_BASE_URL` at this proxy (`http://127.0.0.1:8787`)
- Claude Code sets `ANTHROPIC_API_KEY=not-used` (key value is ignored unless `PROXY_API_KEY` is set)

## Third-party Services

**NVIDIA Developer Portal:**
- URL: `https://build.nvidia.com`
- Purpose: Where developers obtain `NVIDIA_API_KEY` credentials
- No SDK; key is obtained manually and provided via environment variable

**No other third-party SaaS integrations detected** (no Stripe, no SendGrid, no Twilio, no analytics SDKs).

## Internal Services

**None.** The proxy is a standalone single-process service with no internal microservice dependencies, no message queues, and no databases.

## Auth & Security

**Inbound (proxy ← Claude Code clients):**
- Mechanism: Optional static API key check (`PROXY_API_KEY`)
- Header accepted: `x-api-key` OR `Authorization: Bearer <key>`
- Behavior: If `PROXY_API_KEY` is unset, all inbound requests are accepted without auth
- Implementation: `check_auth()` function in `proxy.py` lines 757–768
- Error response: HTTP 401 with Anthropic-shaped error body `{"type":"error","error":{"type":"authentication_error"}}`

**Outbound (proxy → NVIDIA):**
- Mechanism: Bearer token in `Authorization` header on every request
- Key source: `NVIDIA_API_KEY` env var (required; `PROXY_API_KEY` env var is for inbound, not outbound)
- Key location: set on the shared `httpx.AsyncClient` at startup, never logged
- Startup guard: `proxy.main()` raises `SystemExit` if `NVIDIA_API_KEY` is missing (`proxy.py` line 972)

**Secrets handling:**
- `.env.example` documents required/optional env vars (never committed with real values)
- `config.yaml` supports `api_key` fields but the example file advises preferring env vars
- YAML config path: `PROXY_CONFIG` env var → `./config.yaml` → defaults (no secrets file required)

**ID generation:**
- Message IDs: `msg_` + `secrets.token_urlsafe(18)` (`proxy.py` line 169)
- Tool IDs: `toolu_` + `secrets.token_urlsafe(18)` (`proxy.py` line 173)
- Thinking signatures: `proxy-` + `secrets.token_urlsafe(24)` — opaque, does not validate against Anthropic (`proxy.py` line 178)

## Data Storage

**None.** The proxy is stateless:
- No database
- No cache layer (no Redis, Memcached, etc.)
- No file storage (no S3, local disk writes, etc.)
- No session persistence between requests
- The only in-process state is the shared `httpx.AsyncClient` stored on `app.state.nvidia`

## Model Alias Resolution

**Mapping: Claude model names → NVIDIA model IDs**

Configured via three layered sources (merged in order):

1. Hardcoded defaults in `proxy.py` (`DEFAULT_MODEL_ALIASES`, lines 56–65):
   - `claude-3-5-sonnet-20241022` → `nvidia/llama-3.3-nemotron-super-49b-v1.5`
   - `claude-3-7-sonnet-20250219` → same
   - `claude-sonnet-4-20250514` → same
   - `claude-sonnet-4-5` → same
   - `claude-haiku-4-5` → same
   - `claude-opus-4-1` → same

2. `config.yaml` `model_aliases` section (example in `config.example.yaml`):
   - `claude-haiku-4-5` → `nvidia/nvidia-nemotron-nano-9b-v2`
   - `claude-opus-4-1` → `nvidia/llama-3.1-nemotron-ultra-253b-v1`

3. `MODEL_ALIAS_*` environment variables (highest priority per alias):
   - Format: `MODEL_ALIAS_CLAUDE__HAIKU__4__5=nvidia/some-model`
   - `__` → `/`, `_` → `-` in alias key transformation (`proxy.py` lines 102–105)

Fallback: any model name starting with `claude-` or `anthropic/claude-` that has no alias maps to `DEFAULT_MODEL` (`proxy.py` lines 124–126). NVIDIA-native model IDs pass through unchanged.

---

*Integration audit: 2026-05-13*
