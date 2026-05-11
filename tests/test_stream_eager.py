"""SC-002: message_start MUST be emitted before any upstream token.

We stub the NVIDIA httpx client so its streaming POST never resolves.
The proxy must still flush the message_start SSE event immediately —
this is the property that keeps Claude Code's TUI responsive while
slow reasoning models warm up.
"""
import asyncio

import pytest

import proxy
from proxy import stream_response


class _NeverFinishingStream:
    """Async context manager whose body would block forever if awaited."""

    async def __aenter__(self):
        self.status_code = 200

        async def _hang():
            await asyncio.sleep(3600)
            yield ""

        self.aiter_lines = _hang
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _StubNvidia:
    def stream(self, method, url, json=None):
        return _NeverFinishingStream()


class _StubAppState:
    def __init__(self):
        self.nvidia = _StubNvidia()


class _StubApp:
    def __init__(self):
        self.state = _StubAppState()


class _StubRequest:
    def __init__(self):
        self.app = _StubApp()

    async def is_disconnected(self):
        return False


@pytest.mark.asyncio
async def test_message_start_emitted_before_upstream_first_chunk():
    request = _StubRequest()
    body = {"model": "claude-3-5-sonnet-20241022", "stream": True,
            "messages": [{"role": "user", "content": "hi"}]}
    payload = {"model": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
               "messages": [{"role": "user", "content": "hi"}]}

    gen = stream_response(request, body, payload, tool_id_map={})

    # The first yield must complete promptly, even though the upstream
    # producer would block indefinitely if awaited.
    first = await asyncio.wait_for(gen.__anext__(), timeout=2.0)
    assert first.startswith(b"event: message_start\n"), first[:80]

    # Cleanup: cancel the generator so the hanging producer is torn down.
    await gen.aclose()
