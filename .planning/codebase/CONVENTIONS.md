# Conventions

**Analysis Date:** 2026-05-13

## Code Style

**Formatting:**
- No formatter config file detected (no `.prettierrc`, `.flake8`, `setup.cfg`, or `[tool.ruff]` in `pyproject.toml`)
- Line length is permissive — the longest lines in `proxy.py` run to ~90 characters
- Blank lines between top-level functions: 2 blank lines (PEP 8 standard)
- Blank lines inside functions: single blank line to separate logical phases

**Linting:**
- No linter configured in `pyproject.toml` or standalone config files
- `noqa` comments used sparingly:
  - `# noqa: F401` — suppress unused-import warnings for optional dependencies (`h2`, `uvloop`, `httptools`)
  - `# noqa: BLE001` — suppress broad-except in async producer where all errors must be queued

**`from __future__ import annotations`:**
- Used at top of `proxy.py` — enables PEP 563 postponed evaluation so `list[dict]`, `str | None`, etc. work on Python 3.9

**Type annotations:**
- All public functions in `proxy.py` are annotated
- Return types always explicit: `-> str`, `-> dict`, `-> list[dict] | None`, `-> AsyncIterator[bytes]`
- Union types use `X | Y` style (not `Optional[X]` or `Union[X, Y]`)
- Local variable annotations used only where type inference is ambiguous

**Walrus operator (`:=`):**
- Used throughout for inline assignment in conditionals:
  ```python
  if (rc := delta.get("reasoning_content")):
  if (m := THINK_RE.search(content)):
  ```

**String style:**
- Double-quoted strings everywhere
- f-strings for interpolation; no `%` or `.format()`

## Naming Conventions

**Functions:**
- `snake_case` for all functions
- Prefix `_` for module-private helpers: `_load_yaml_config()`, `_safe_suffix_len()`, `_close_open()`, `_open_text()`, `_emit_text()`, `_process_text()`
- Verb-first names: `resolve_model()`, `translate_request()`, `translate_messages()`, `translate_tools()`, `translate_response()`, `encode_sse()`, `map_error_type()`, `check_auth()`

**Variables:** `snake_case`; short names in tight loops (`m`, `r`, `tc`, `fn`, `ev`)

**Constants:** `UPPER_SNAKE_CASE` — `NVIDIA_API_KEY`, `PROXY_HOST`, `PING_INTERVAL`, `TEXT_DELTA_CHARS`

**Classes:** `PascalCase` (`StreamTranslator`); test stub classes use underscore prefix (`_StubNvidia`, `_StubRequest`)

**Lookup tables:** `UPPER_SNAKE_CASE` with `_TO_` separator: `FINISH_TO_STOP`, `STATUS_TO_ERR`

**Compiled regex:** `UPPER_SNAKE_CASE` with `_RE` suffix: `SERVER_TOOL_RE`, `THINK_RE`, `_SPLIT_RE`

## File Organization

**Single-module design:**
- `proxy.py` — entire FastAPI proxy (~1000 lines); translation logic, streaming state machine, HTTP routes, entrypoint
- `nim_code.py` — CLI wrapper; subprocess-level lifecycle orchestration
- `tests/` — flat directory, no subdirectories

**Section ordering in `proxy.py`** marked with ASCII banner comments:
1. Module docstring + imports
2. Config constants and `_load_yaml_config()`
3. `resolve_model()`
4. Lifespan context manager + `app = FastAPI(...)`
5. ID helpers (`new_msg_id`, `new_tool_id`, `new_signature`)
6. Anthropic-to-OpenAI request translation functions
7. Non-streaming response translation
8. Streaming state machine (`StreamTranslator` class)
9. HTTP routes
10. `main()` entrypoint

**ASCII section banners:**
```python
# ─── Config ──────────────────────────────────────────────────────────────────
# ─── Streaming state machine ─────────────────────────────────────────────────
# ─── HTTP routes ─────────────────────────────────────────────────────────────
```

**Imports:** Standard library first, third-party second (PEP 8). Specific `from` imports; no wildcards.

## Error Handling Patterns

**HTTP error response shape** (always consistent):
```python
{"type": "error", "error": {"type": "...", "message": "..."}}
```

**Error type resolution:** `map_error_type(status_code)` → `STATUS_TO_ERR` dict; unknown codes → `"api_error"`

**Message truncation:** All upstream error text capped at 500 chars: `str(e)[:500]`

**HTTP status preservation:** Upstream 4xx/5xx codes forwarded unchanged on the response

**Exception handling in routes:**
- JSON decode failure → `except Exception as e` → 400
- Network error on non-streaming POST → `except httpx.HTTPError as e` → 502
- Upstream error status detected by `resp.status_code >= 400`; error extracted from `.json().get("detail")` or `.json().get("message")` or `.text`

**Async producer error propagation:**
- Errors inside `producer()` coroutine are caught and queued as `(ERROR, e)` sentinel tuples
- Consumer loop checks: `if isinstance(item, tuple) and item and item[0] is ERROR`
- Prevents exception loss across `asyncio.Task` boundaries

**Startup guards:**
- `lifespan()` raises `RuntimeError` if `NVIDIA_API_KEY` absent
- `main()` raises `SystemExit` for same check in CLI path

**Silent drops (always commented):**
- `thinking`, `redacted_thinking`, `document` content blocks dropped with inline rationale
- Anthropic server-side tool types matching `_20YYYYMMDD$` dropped via `SERVER_TOOL_RE`
- `json.JSONDecodeError` on SSE lines → `continue`

## Logging Patterns

**Framework:** `print()` — no structured logging library

**Startup messages** to stdout only. No per-request logging at runtime.

**Log level:** Passed to `uvicorn` via `log_level=LOG_LEVEL` (env default `"info"`); access logs suppressed (`access_log=False`)

**CLI output in `nim_code.py`:** Emoji-prefixed `print()` for human-readable status; `sys.exit(1)` on fatal errors

## Documentation Style

**Module docstring** at top of `proxy.py` covers purpose, required/optional env vars, and shell run instructions.

**Function docstrings:** Used selectively for non-obvious behavior. Short summary sentence first; extended prose if needed.

**Inline comments:** Explain decisions and rationale, not mechanics. Numbered for ordered constraints:
```python
# 1) Emit message_start IMMEDIATELY — don't wait for NVIDIA's first chunk.
# 2) Ping every 15 s while idle so Claude Code's TUI never freezes.
# 3) Cancel the upstream task on client disconnect.
```

**No TODO/FIXME markers** in `proxy.py`; rationale comments preferred over deferred notes.
