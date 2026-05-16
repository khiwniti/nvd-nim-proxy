# Changelog

All notable changes to **nim-claude-proxy** are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

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
