# Changelog

All notable changes to **nim-claude-proxy** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

---

## [0.3.0] — 2026-07-08

### Added
- **Stream budget**: hard wall cap on a single `/v1/messages` exchange
  (`PROXY_STREAM_BUDGET_SECONDS`, default 600 s). When the budget elapses the
  proxy emits a clean `stop_reason=max_tokens` + `message_stop` pair rather than
  letting the upstream socket hang or RST — Claude Code no longer reports
  "proxy force quit" on long reasoning turns.
- **Lazy NVIDIA_API_KEY**: missing key no longer aborts lifespan. The proxy
  starts so `nim doctor` can probe `/healthz`, and routes return the structured
  `authentication_error` 503 with a configuration hint (set the env var or
  `wrangler secret put NVIDIA_API_KEY`) when the key is absent.
- **Parallel pre-flight probes**: lifespan now fans out `/chat/completions`
  probes to unique NVIDIA alias targets with a Semaphore-bounded fanout
  (`PROXY_PREFLIGHT_CONCURRENCY=4`), per-phase read timeout
  (`PROXY_PREFLIGHT_TIMEOUT_S=6.0`), and total wall cap
  (`PROXY_PREFLIGHT_TOTAL_S=8.0`). Startup wall time is bounded even when
  NVIDIA's edge is slow — sequential probes used to risk ~200 s stalls.
- **stop_sequence detection**: when the client supplies `stop_sequences` and
  the cumulative visible text still ends with one, both the streaming
  (`StreamTranslator.finalize`) and non-streaming (`translate_response`) paths
  now upgrade `end_turn` → `stop_sequence` and echo the matching sequence in
  `stop_sequence`. A false positive is worse than a miss so detection is
  suffix-only.
- **Prompt-cache token echo**: NVIDIA's `usage.prompt_tokens_details.cached_tokens`
  is now mapped to Anthropic's `usage.cache_read_input_tokens` (and subtracted
  from `input_tokens` per Anthropic's convention), so Claude Code's
  cache-credit counter stays accurate instead of sticking at zero.
- **Drain safety net for parallel tool calls**: any `tool_use` block whose
  `content_block_start` was emitted but never received a matching
  `content_block_stop` (e.g., when the upstream truncates mid-write after
  opening a second tool) now always emits a validated `input_json_delta` +
  `content_block_stop` pair. Prevents the "spinner never stops" / force-quit
  hang when the stream dies in pathological states.
- **Hardened SSE terminal**: `stream_response` now always emits a final
  `message_stop` even when translate or finalize raises partway through.
  Replaces the silent truncation that previously surfaced as a Claude Code
  "force quit".
- **Bounded uvicorn shutdown**: `timeout_grace_time = PROXY_STREAM_BUDGET_SECONDS + 30`
  is set on the uvicorn `Server`, and `app._uvicorn_server` is bound so the
  SIGTERM handler can flip `should_exit`. Docker / Cloudflare Containers no
  longer hit the 10 s default SIGKILL before SSEs drain.
- **Cloudflare Worker timing-safe API key compare**: `authorized()` no longer
  uses `===` against `PROXY_API_KEY` (timing-attack susceptible); both length
  and byte equality are constant-time.
- **Regression tests** covering: stop_sequence detection (both promotion and
  no-false-positive paths); drain of started-but-unclosed tool blocks;
  inline `<think>` truncation becoming a thinking block; `message_stop`
  ordering invariant under partial-failure.

### Changed
- `app.version` value pinned to `"0.3.0"` (was the stale `"1.0.0"` that didn't
  match the printed banner or version history).
- `count_tokens` heuristics docstring clarifies the err-high rationale; ratio
  is unchanged at chars/4 but the comment now reflects that NVIDIA tokenizers
  average ~3.5 chars/token so under-estimation is more dangerous than
  over-estimation.
- Pre-flight probe target set is now deduplicated (`set(MODEL_ALIASES.values())`)
  to bound fanout at the number of *unique upstream models*, not the number
  of alias entries.

### Fixed
- **Force-quit during long reasoning turns**: combination of the budget cap,
  hardened SSE terminal, drain safety net, and bounded uvicorn shutdown makes
  the proxy survive 600 s+ Nemotron reasoning streams without dropping the
  SSE.
- **Stop sequence lost in translation**: previously both paths always reported
  `stop_reason: end_turn` even when a custom sequence fired — now correctly
  disambiguated.
- **Cached-token credit stuck at zero**: cache hits now surface in
  `usage.cache_read_input_tokens`.
- **Cache-token double-count**: `input_tokens` no longer double-counts the
  cached portion (subtracted from prompt total, clamped at zero).
- **Banner/version drift**: removed the duplicate `v1.0` print that followed
  the `v0.3.0` banner.

---

## [0.2.8] — 2026-05-24

### Changed
- Patch release for prompt-cache token echo path. No behavior change visible
  to Claude Code.

---

## [0.2.6] — 2026-05-16

### Fixed
- **Streaming tool call parse failures — permanent buffer-and-validate**: 0.2.5
  added a `{}` fallback when zero arg chunks were emitted, but mid-stream
  truncation (max_tokens cap, network drop, model quirk leaving an unclosed
  quote or brace) still produced unparseable `partial_json` at
  `content_block_stop`. `StreamTranslator` now buffers all `function.arguments`
  fragments per tool block, then emits **one atomic `input_json_delta`** at
  block close whose contents are guaranteed parseable by `JSON.parse()`.
- **`_validate_or_repair_tool_args()`** auto-repair: empty/whitespace becomes
  `{}`; valid JSON passes through; truncated JSON gets its open string closed
  and brackets/braces re-balanced in LIFO order before a re-parse; if repair
  still fails the helper returns `{}` as a safe fallback. Trades fine-grained
  delta streaming for correctness — the trade is invisible to Claude Code,
  which only parses at `content_block_stop`.

---

## [0.2.5] — 2026-05-16

### Fixed
- **Streaming tool call parse failure** ("The model's tool call could not be parsed"):
  NVIDIA's first streaming chunk for a tool call always has `"arguments": null`; if a
  second chunk with real args never arrives (no-arg tool, model quirk, or network race),
  `StreamTranslator` emitted zero `input_json_delta` events. Claude Code then called
  `JSON.parse("")` → threw → showed the parse-error dialog. Fixed by tracking whether
  any args delta was emitted per tool block and injecting a single `{}` fallback in
  `_close_open()` before `content_block_stop` when args were never sent.
- **Dict arguments guard**: NVIDIA could theoretically send `function.arguments` as a
  pre-parsed JSON object (dict) instead of a JSON string, violating the OpenAI streaming
  spec. Added `isinstance(args, dict)` check with automatic `json.dumps()` conversion so
  `partial_json` is always a string.

---

## [0.2.4] — 2026-05-16

### Fixed
- **Context-overflow root cause**: tool definitions (60+ schemas Claude Code sends)
  were not counted in the pre-flight token estimate, causing systematic 400 errors
  on large sessions. `translate_request` now pre-translates tools and includes them
  in `_count_chars` before clamping `max_tokens`.
- **Retry resilience**: NVIDIA's `"at least N input tokens"` in overflow errors is
  a lower bound, not an exact count. Streaming path retries raised from 2 → 3
  attempts; non-streaming from 1 → 2 retries. Each successive attempt adds
  `attempt × 4096` extra margin to absorb the undercount.
- `CONTEXT_SAFETY_MARGIN` default raised from 2 048 → **4 096** tokens so that
  normal tokenizer drift no longer reaches NVIDIA before the proxy catches it.

### Added
- `MAX_REQUEST_BODY` limit (default 10 MB) — rejects oversized payloads with 413
  before JSON parsing to prevent memory pressure.
- Cloudflare `wrangler.toml` hardening: `[observability]`, `[placement] mode = "smart"`,
  `instance_type = "standard"`, 25 % gradual rollout (`rollout_kind = "full_auto"`).
- Second "Deploy to Cloudflare" button inside the Deploy section of `README.md`.

---

## [0.2.3] — 2026-05-16

### Added
- `nim kill [--port PORT]` command to free the proxy port by terminating the
  process currently listening on it.

### Changed
- Port-in-use errors now suggest `nim kill --port <port>` as the direct fix.

---

## [0.2.2] — 2026-05-16

### Fixed
- `nim code --model ...` and interactive model selection now set
  `ANTHROPIC_MODEL` in addition to picker/default model variables, so Claude
  Code immediately starts with the selected NVIDIA model instead of showing the
  configured default.

---

## [0.2.1] — 2026-05-16

### Added
- Cloudflare Workers + Containers deployment support with root `wrangler.toml`,
  Worker entrypoint, typecheck config, and a README deploy button.
- Claude Code gateway alignment for `nim code`: gateway model discovery,
  disabled Anthropic-only beta/tool-reference/thinking paths, and `/health`
  alias alongside `/healthz`.
- Context-window controls: `MAX_OUTPUT_TOKENS`, `CONTEXT_SAFETY_MARGIN`, YAML
  config examples, and one-shot retry after NVIDIA tokenizer overflow errors.
- Security headers on FastAPI responses and Cloudflare Worker edge responses.
- Tests covering context-overflow retry, auth enforcement, and `/health` alias.
- Interactive model picker shown before `nim code` launches — 20 flagship models
  from NVIDIA, DeepSeek, Qwen, Mistral, Z-AI, MiniMax, Moonshot, Meta, Google,
  OpenAI OSS, ByteDance, StepFun, and Writer; type a number, a model ID, or
  Enter for the default.
- `FLAGSHIP_MODELS` catalogue in `nim_code.py`; easily extended.
- `pick_model_interactive()` helper — skipped when `--model` is passed or stdin
  is non-TTY (scripts, CI).
- Per-tier model config: `nvidia.opus_model` and `nvidia.haiku_model` in
  `config.yaml` set the Opus and Haiku slots in Claude Code's own picker.
- `get_tier_models()` helper; env-var overrides `OPUS_NVIDIA_MODEL` /
  `HAIKU_NVIDIA_MODEL` take precedence over config.
- `CLAUDE_CODE_SUBAGENT_MODEL` wired to the Haiku-tier model (fast model for
  background sub-agents).

### Fixed
- `PROXY_API_KEY` enforcement now returns auth failures from `/v1/messages` and
  `/v1/messages/count_tokens` instead of continuing request handling.
- README PyPI package references now use `nim-claude-proxy`, matching the
  published package name.
- `nim version` now reads the installed `nim-claude-proxy` distribution.

### Changed
- Docker image now runs as a non-root `app` user with Python runtime hardening.
- Default Opus slot: `deepseek-ai/deepseek-v4-pro` (was NVIDIA Ultra 253B).
- Default Haiku slot: `minimaxai/minimax-m2.7` (was nano-9b-v2).
- `config.example.yaml` documents all three tier-model fields.

---

## [0.2.0] — 2025-05-14

### Added
- `nim use <model>` — switch default model instantly; restarts daemon if running.
- `nim models` — list available NVIDIA NIM models via the proxy.
- CI/CD via GitHub Actions:
  - `ci.yml` — pytest matrix on Python 3.9 / 3.11 / 3.12 on push and PR.
  - `publish.yml` — auto-publish to PyPI on `v*` tags using `PYPI_TOKEN` secret.
- `_nvidia_error_message()` helper — correctly extracts the message from
  NVIDIA's nested `{"error":{"message":"..."}}` envelope.
- Eager `message_start` emission before any upstream await (sub-100 ms TTFT).
- 15 s ping heartbeat during long reasoning turns to keep the TUI alive.
- HTTP/2 to NVIDIA when the `h2` package is installed.
- Client-disconnect cancellation via `asyncio` task cleanup.
- Token counting endpoint `/v1/messages/count_tokens` (heuristic ±15 %).
- `nimr` / `nim-proxy` CLI entry points in `pyproject.toml`.
- Rich-styled terminal output throughout the CLI (panels, tables, spinners).
- Production daemon: PID file at `~/.config/nim-proxy/nim-proxy.pid`,
  SIGTERM → wait → SIGKILL lifecycle, `start_new_session=True` for detachment.
- `nim init`, `nim start`, `nim stop`, `nim restart`, `nim status`,
  `nim logs [-f] [-n N]`, `nim doctor`, `nim configure`, `nim test`,
  `nim proxy`, `nim version` commands.
- Global config at `~/.config/nim-proxy/config.yaml`; env vars override YAML.
- `PROXY_API_KEY` optional client-facing auth with `hmac.compare_digest`.
- 20 tests covering translation, streaming, routes, and error extraction.

### Fixed
- `ORJSONResponse` deprecation — replaced with `JSONResp()` helper using
  `Response(content=orjson.dumps(...), media_type="application/json")`.
- Timing attack in `check_auth()` — plain `!=` replaced with
  `hmac.compare_digest()`.
- `/v1/models` crashing on non-JSON upstream responses.
- `image_block_to_openai()` raising unhandled `ValueError` on unknown source types.
- Streaming producer: error body now sent as a `(ERROR, status, body)` queue
  tuple instead of baked into an exception string.
- Leaked real NVIDIA API key redacted from `.env.example`.

### Changed
- Package renamed from `nvd-claude-nim` → `nim-claude-proxy` (PyPI conflict).
- Package version bumped `0.1.5` → `0.2.0`.
- Author updated to `khiwniti`.
- `rich>=13.0` added to dependencies.
- README fully rewritten: badges, ASCII architecture diagram, quickstart,
  CLI reference table, model recommendations, feature matrix, troubleshooting.

---

## [0.1.5] — initial development

### Added
- YAML config loader and model alias resolution.
- Request/response translation: Anthropic Messages API → OpenAI Chat Completions.
- Streaming SSE translation (`StreamTranslator`).
- Tool call round-trip (`tool_use` ↔ `tool_calls`).
- System prompt handling (string and block-array forms).
- Vision support (base64 + URL image blocks).
- Reasoning block support (`reasoning_content` + `<think>` tag stripping).
- Spec Kit artifacts, initial test suite, and Git extension scaffolding.
