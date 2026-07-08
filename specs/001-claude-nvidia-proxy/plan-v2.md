# Plan v2 — nvd-nim-proxy 0.3.0 (Stability + Protocol Polish)

**Branch**: `001-claude-nvidia-proxy` (continues v1 plan)
**Date**: 2026-07-03
**Source plan**: [plan.md](./plan.md)
**Spec**: [spec.md](./spec.md)
**Supersedes**: Only the bugs and rows below. Existing FRs/SCs unchanged.

## Why this revision exists

Telemetry from local Claude Code sessions repeatedly shows three classes of symptom that the 0.2.x line does not address:

1. **Sudden SSE close during long sessions** — Claude Code displays "force quit" or "proxy stopped responding" because the proxy's own shutdown handler kills the process abruptly (`os._exit(0)` after a 10 s timer thread), and because the lifespan pre-flight can stall startup for **up to ~200 s** on slow NVIDIA endpoints (sequential probe of ~25 alias targets × 8 s timeout).
2. **Hard-fail on missing `NVIDIA_API_KEY` during Cold Start** — `lifespan` raises `RuntimeError`, the daemon never starts, and `nim doctor` gives no actionable hint.
3. **OpenAI→Anthropic protocol gaps** that surface as session accounting drift in Claude Code:
   - parallel `tool_calls` (OpenAI emits two `o_idx` in the same delta) → second tool's `input_json_delta` is emitted on the *wrong* index because the loop "validate and break" picks the first matching buffer
   - stream truncates mid-response → the recovered `{}` is great, but no `stop_reason="max_tokens"` event carries the truncation signal back to Claude Code when the proxy synthesises the empty `{}`
   - `cache_control` echo is absent, so the UI credit counter for cached tokens stays at zero even when upstream did cache

This plan executes the **Stability + Protocol Polish** lane in 9 ordered tasks, ships 0.3.0, and keeps the file-pace change small enough to review in one sitting.

## Concrete code-change map

| ID | File | Lines (current) | Change |
|----|------|-----------------|--------|
| F1 | `proxy.py` | 188–209 (`_install_graceful_shutdown_handlers`) | Replace `threading.Thread(... os._exit(0) ...)` with uvicorn's first-class `SignalHandlers` + `force_exit=False`; drain active SSEs by polling `app.state.active_streams` |
| F2 | `proxy.py` | 214, 1634 (`raise RuntimeError`/`SystemExit`) | Treat missing `NVIDIA_API_KEY` as a lazy check inside `/v1/messages` and `/v1/messages/count_tokens`; emit 503 with `authentication_error` until set |
| F3 | `proxy.py` | 212–259 (`lifespan`) | Run pre-flight probes with `asyncio.Semaphore(4)` and a 6 s per-call `asyncio.wait_for`; total wall ≤ 8 s; emit `app.state.blocked_models` even when nothing probed (set() stays empty) |
| F4 | `proxy.py` | 836–857 (`StreamTranslator._close_open`) | Validate *every* started buffer that hasn't yet emitted, not just the matching one, by tracking `validated_idx: set[int]`; emit only buffers whose `anth_idx` was emitted since last validation pass |
| F5 | `proxy.py` | 1499–1552 (`producer`) | Add overall `MAX_PROXY_STREAM_DURATION_SECONDS=600` wall cap; on expiry emit `message_delta` (`stop_reason=max_tokens`) + `message_stop` and cancel upstream |
| F6 | `proxy.py` | 1306–1313 (`/v1/models`) | Echo static `created_at` for alias entries (`claude-*` shapes) and forward the real `created` for NVIDIA native models; add `display_name` from `MODEL_ALIASES` keys for nicer picker UX |
| F7 | `proxy.py` | 1246–1249 (`/healthz`) | Return multiple component statuses (`key_configured`, `upstream_reachable`, `models_loaded`) but stay backwards-compatible (`status: ok` retained at top level) |
| F8 | `proxy.py` | 660–672 (`translate_response`) | Detect `stop_sequences` in the cumulative streaming text and emit `stop_reason = "stop_sequence"` for the matching client-supplied sequence |
| F9 | `proxy.py` | 928–963 (`_process_text`) | Detect `<think>…</think>` truncation (opening tag without closing tag at `finish`) and emit a final `thinking` block + natural signature instead of dropping the buffer |
| T1 | `tests/test_streaming.py` | — | Add `test_parallel_tool_calls_emit_distinct_blocks`, `test_close_open_emits_pending_validated_for_every_started_buf` |
| T2 | `tests/test_translation.py` | — | Add `test_stop_sequence_detected_in_response_text`, `test_lazy_key_returns_503_from_messages`, `test_lifespan_with_pre_flight_bounded` |
| T3 | `proxy.py` | new helper `_echo_cache_tokens(...)` | Map upstream `prompt_tokens_details.cached_tokens` → `usage.cache_read_input_tokens` |
| T4 | `config.example.yaml`, `.env.example`, `README.md` | — | Document new env vars: `PROXY_STREAM_BUDGET_SECONDS`, `PROXY_PREFLIGHT_CONCURRENCY`, `PROXY_PREFLIGHT_TIMEOUT_S` |
| T5 | `pyproject.toml`, `CHANGELOG.md` | version `0.2.6` → `0.3.0` | Add `### Added / ### Fixed / ### Changed` sections describing the 9 items |

## Acceptance checklist (one-shot for this plan)

- [ ] `python3 -m pytest -q` passes with all new tests added (no live NVIDIA calls; uses `respx`).
- [ ] `python3 proxy.py` now starts even without `NVIDIA_API_KEY`; first `/v1/messages` returns **503** with `authentication_error` and a hint about `wrangler secret put` / `nim configure`.
- [ ] Startup wall time ≤ 10 s on a clean network (currently undocumented, often 30–60 s).
- [ ] Sending a parallel-tool request (two `tool_calls` indices in the same delta) yields two Anthropic `tool_use` blocks, each with a single parseable `input_json_delta`.
- [ ] `SIGTERM` (or Ctrl-C) leaves active SSE streams connected long enough to deliver a final `message_stop`; no `os._exit` thread is spawned.
- [ ] Long-running thoughts past 600 s now end cleanly with `stop_reason=max_tokens` (configurable) instead of a TCP RST.

## Risks & rollbacks

- **F1 changes shutdown semantics** in a way that may affect Docker `--stop-timeout` defaults; test on a Linux system before publishing.
- **F4 changes how `validated_idx` is keyed** — must be tested against the 0.2.6 buffer-and-validate behaviour to avoid regressing the parse-failure fix.
- **F3 tighter pre-flight** trades "early detection of access denial" for speed. The endpoint reached on first request will still surface access denial to Claude Code (existing path), so no feature loss.

## Out of scope (deliberately)

- LiteLLM swap-in
- Anthropic prompt caching semantics beyond token echo (i.e., no rewrite of system+tools to introduce `cache_control` markers; that path is closed until NVIDIA exposes a matching cache-control API)
- A non-streaming fast-path for `/v1/messages` (already lean)
- New model families beyond what's in `MODEL_ALIASES` today
