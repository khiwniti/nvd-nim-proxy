# Testing

**Analysis Date:** 2026-05-13

## Test Framework & Tools

**Runner:** pytest >= 8.0 (`requirements.txt`)

**Async support:** pytest-asyncio >= 0.23 — required for `@pytest.mark.asyncio` tests in `tests/test_stream_eager.py`

**HTTP mocking:** respx >= 0.21 — mocks `httpx.AsyncClient` requests; used in `tests/test_e2e.py` via the `respx_mock` fixture

**Coverage:** pytest-cov >= 5.0 available; no coverage threshold configured in `pyproject.toml`

**Run commands:**
```bash
python3 -m pytest              # run all 16 tests
python3 -m pytest --tb=short   # with short tracebacks
python3 -m pytest --co -q      # list all collected tests without running
python3 -m pytest tests/test_translation.py  # single file
```

No `[tool.pytest.ini_options]` section in `pyproject.toml` — pytest runs with default discovery.

## Test Structure

**Location:** All tests live in `tests/` (flat, no subdirectories)

**Files:**
- `tests/conftest.py` — module-level setup; sets `NVIDIA_API_KEY` env var before proxy import
- `tests/test_translation.py` — pure unit tests for translation functions
- `tests/test_streaming.py` — unit tests for `StreamTranslator` state machine and SSE encoding
- `tests/test_stream_eager.py` — async test for eager `message_start` emission (SC-002)
- `tests/test_routes.py` — FastAPI route smoke tests using `TestClient`
- `tests/test_e2e.py` — end-to-end tests using `TestClient` + `respx_mock`

**Test naming:** `test_<what>_<condition_or_outcome>()` — all lowercase snake_case

**Assertion style:** Plain `assert` with no helper library. Assertions are direct and specific:
```python
assert names[:2] == ["content_block_start", "content_block_delta"]
assert first.startswith(b"event: message_start\n"), first[:80]
assert data["error"]["type"] == "rate_limit_error"
```

## Test Categories

**Unit tests — translation logic** (`tests/test_translation.py`, 6 tests):
- Test `resolve_model()` alias mapping and passthrough
- Test `translate_request()` end-to-end field mapping including tool filtering, tool protocol injection, and metadata forwarding
- Test `translate_messages()` with tool_result round-trip
- Test `translate_response()` for tool_use block construction and stop_reason mapping
- Test `map_error_type()` for HTTP status code → Anthropic error type string

**Unit tests — streaming state machine** (`tests/test_streaming.py`, 4 tests):
- Test `StreamTranslator.feed()` + `finalize()` event sequence for text content
- Test inline `<think>...</think>` tag handling producing separate thinking and text blocks
- Test tool_call delta producing `content_block_start` (tool_use) and `content_block_delta` (input_json_delta)
- Test `encode_sse()` byte output format

**Async behavioral test — eager message_start** (`tests/test_stream_eager.py`, 1 test):
- Verifies SC-002 spec: `message_start` SSE event is yielded before any upstream token arrives
- Uses manual stub objects (not respx) to simulate a never-resolving upstream stream
- Uses `asyncio.wait_for(..., timeout=2.0)` to assert the first yield is prompt

**Route smoke tests** (`tests/test_routes.py`, 2 tests):
- Test `/healthz` returns `{"status": "ok"}` with 200
- Test `/v1/messages` with missing required fields returns 400 with correct error shape

**End-to-end integration tests** (`tests/test_e2e.py`, 3 tests):
- Non-streaming: full Anthropic Messages request → mocked NVIDIA response → Anthropic response shape verified
- Streaming: full SSE stream verified; `message_start`, text deltas, `message_stop` all present and content correct
- Error propagation: upstream 429 → response 429 with `rate_limit_error` type

## Coverage

**No coverage threshold configured.** `pytest-cov` is installed but not wired to any `pyproject.toml` `[tool.coverage]` section.

**Run with coverage:**
```bash
python3 -m pytest --cov=proxy --cov-report=term-missing
```

**Covered areas:** Translation functions, StreamTranslator state machine, SSE encoding, route validation, error mapping, eager streaming behavior, NVIDIA API mocking.

**Not covered by tests:** `nim_code.py` CLI commands, `lifespan()` startup/shutdown path, `/v1/models` proxy route, `/v1/messages/count_tokens` route, `stream_response()` ping/disconnect paths, PROXY_API_KEY authentication (`check_auth()`).

## Running Tests

```bash
# From repo root
python3 -m pytest

# Single category
python3 -m pytest tests/test_translation.py
python3 -m pytest tests/test_streaming.py
python3 -m pytest tests/test_e2e.py

# With coverage report
python3 -m pytest --cov=proxy --cov-report=term-missing

# Verbose output
python3 -m pytest -v
```

All 16 tests pass in ~1.4s on Python 3.14. Four deprecation warnings are emitted by FastAPI regarding `ORJSONResponse` but do not affect test outcomes.

## Test Patterns & Fixtures

**conftest.py global setup:**
```python
# tests/conftest.py
import os
os.environ.setdefault("NVIDIA_API_KEY", "test-key-for-pytest")
```
This runs before any test module imports `proxy`, preventing the lifespan startup guard from raising `RuntimeError`.

**TestClient fixture pattern** (`tests/test_e2e.py`):
```python
@pytest.fixture
def client():
    import os
    os.environ["NVIDIA_API_KEY"] = "test-key"
    with TestClient(app) as c:
        yield c
```
- Uses `fastapi.testclient.TestClient` as context manager to run the FastAPI lifespan
- `raise_server_exceptions=False` used in `tests/test_routes.py` to surface lifespan errors as responses

**respx mocking pattern** (`tests/test_e2e.py`):
```python
def test_e2e_non_streaming(client, respx_mock):
    respx_mock.post("https://integrate.api.nvidia.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={...})
    )
```
- `respx_mock` is a built-in pytest fixture from `respx`; no explicit import needed
- The full NVIDIA URL must be specified (matches `NVIDIA_BASE_URL` default)

**Manual stub pattern for async tests** (`tests/test_stream_eager.py`):
```python
class _NeverFinishingStream:
    async def __aenter__(self):
        self.status_code = 200
        async def _hang():
            await asyncio.sleep(3600)
            yield ""
        self.aiter_lines = _hang
        return self
    async def __aexit__(self, exc_type, exc, tb):
        return False
```
Used instead of respx when the test needs to control async timing precisely.

**Async test pattern** (`tests/test_stream_eager.py`):
```python
@pytest.mark.asyncio
async def test_message_start_emitted_before_upstream_first_chunk():
    gen = stream_response(request, body, payload, tool_id_map={})
    first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    assert first.startswith(b"event: message_start\n"), first[:80]
    await gen.aclose()
```
- `@pytest.mark.asyncio` required; no `asyncio_mode = "auto"` configured
- Generator cleanup via `await gen.aclose()` ensures hanging producer task is cancelled

**Direct function testing pattern** (`tests/test_streaming.py`):
```python
def test_stream_translator_text_event_order():
    st = proxy.StreamTranslator("nvidia/model", {})
    chunk = {"choices": [{"delta": {"content": "hello world"}, "finish_reason": None}]}
    events = list(st.feed(chunk))
    ...
    names = [e["event"] for e in events]
    assert names[:2] == ["content_block_start", "content_block_delta"]
```
- `StreamTranslator` is instantiated directly; no FastAPI app needed
- Feed chunks incrementally, accumulate events, then assert on event sequence
