import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

import proxy
from proxy import app


@pytest.fixture
def client():
    # Ensure NVIDIA_API_KEY is set for lifespan to succeed
    import os

    os.environ["NVIDIA_API_KEY"] = "test-key"
    with TestClient(app) as c:
        yield c


def test_e2e_non_streaming(client, respx_mock):
    # Setup mock
    respx_mock.post("https://integrate.api.nvidia.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "chat-123",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Hello! I am a mock AI.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )
    )

    response = client.post(
        "/v1/messages",
        json={
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["type"] == "message"
    assert data["content"][0]["text"] == "Hello! I am a mock AI."
    assert data["model"] == "claude-3-5-sonnet-20241022"


def test_e2e_streaming(client, respx_mock):
    # Setup mock stream
    stream_content = [
        "data: "
        + json.dumps(
            {"choices": [{"delta": {"role": "assistant", "content": "Hello"}}]}
        )
        + "\n\n",
        "data: "
        + json.dumps({"choices": [{"delta": {"content": " world!"}}]})
        + "\n\n",
        "data: "
        + json.dumps(
            {
                "choices": [{"finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            }
        )
        + "\n\n",
        "data: [DONE]\n\n",
    ]
    respx_mock.post("https://integrate.api.nvidia.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, content="".join(stream_content))
    )

    with client.stream(
        "POST",
        "/v1/messages",
        json={
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "stream": True,
            "messages": [{"role": "user", "content": "Hi"}],
        },
    ) as response:
        assert response.status_code == 200
        events = []
        for line in response.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

        assert any(e["type"] == "message_start" for e in events)

        # Verify the accumulated text content
        text_deltas = [
            e["delta"]["text"]
            for e in events
            if e["type"] == "content_block_delta" and e["delta"]["type"] == "text_delta"
        ]
        full_text = "".join(text_deltas)
        assert full_text == "Hello world!"

        assert any(e["type"] == "message_stop" for e in events)


def test_e2e_error_propagation(client, respx_mock):
    respx_mock.post("https://integrate.api.nvidia.com/v1/chat/completions").mock(
        return_value=httpx.Response(429, json={"detail": "Too many requests"})
    )

    response = client.post(
        "/v1/messages",
        json={
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
        },
    )

    assert response.status_code == 429
    data = response.json()
    assert data["type"] == "error"
    assert data["error"]["type"] == "rate_limit_error"


def test_context_overflow_retries_with_reduced_max_tokens(client, respx_mock):
    route = respx_mock.post("https://integrate.api.nvidia.com/v1/chat/completions")
    route.side_effect = [
        httpx.Response(
            400,
            json={
                "error": {
                    "message": (
                        "This model's maximum context length is 131072 tokens. "
                        "However, you requested 16382 output tokens and your prompt contains at "
                        "least 114691 input tokens."
                    )
                }
            },
        ),
        httpx.Response(
            200,
            json={
                "id": "chat-124",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Recovered."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 114691, "completion_tokens": 16},
            },
        ),
    ]

    response = client.post(
        "/v1/messages",
        json={
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 16382,
            "messages": [{"role": "user", "content": "large prompt"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "Recovered."
    assert len(route.calls) == 2
    assert route.calls[1].request.content
    retry_payload = json.loads(route.calls[1].request.content)
    assert retry_payload["max_tokens"] == 131072 - 114691 - proxy.CONTEXT_SAFETY_MARGIN


# NVIDIA marks a hosted model's "function" DEGRADED when its backend is
# unhealthy. The catalog still lists the model, so the request routes to it and
# NVIDIA returns 400 "Function id '<uuid>': DEGRADED function cannot be invoked".
_DEGRADED_MSG = (
    "Function id '87ea0ddc-cff1-4bca-bf8b-3bd98a35ddd0': "
    "DEGRADED function cannot be invoked."
)


def test_degraded_model_auto_falls_back_non_streaming(client, respx_mock, monkeypatch):
    # Route a non-default model to a distinct upstream target so we can prove
    # the retry switches to DEFAULT_MODEL rather than re-hitting the same one.
    monkeypatch.setitem(proxy.MODEL_ALIASES, "claude-opus-4-1", "deepseek-ai/deepseek-v4-pro")
    monkeypatch.setattr(proxy, "DEFAULT_MODEL", "nvidia/llama-3.3-nemotron-super-49b-v1.5")

    route = respx_mock.post("https://integrate.api.nvidia.com/v1/chat/completions")
    route.side_effect = [
        httpx.Response(400, json={"error": {"message": _DEGRADED_MSG}}),
        httpx.Response(
            200,
            json={
                "id": "chat-degraded",
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "Fell back OK."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": 3},
            },
        ),
    ]

    response = client.post(
        "/v1/messages",
        json={
            "model": "claude-opus-4-1",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
        },
    )

    assert response.status_code == 200
    assert response.json()["content"][0]["text"] == "Fell back OK."
    # Second call must target the healthy DEFAULT_MODEL, not the degraded one.
    assert len(route.calls) == 2
    retry_payload = json.loads(route.calls[1].request.content)
    assert retry_payload["model"] == "nvidia/llama-3.3-nemotron-super-49b-v1.5"


def test_degraded_model_error_when_already_default_non_streaming(client, respx_mock, monkeypatch):
    # If the degraded model IS already the default, there's nothing healthier to
    # fall back to — surface a clear error instead of looping.
    monkeypatch.setattr(proxy, "DEFAULT_MODEL", "nvidia/llama-3.3-nemotron-super-49b-v1.5")
    route = respx_mock.post("https://integrate.api.nvidia.com/v1/chat/completions")
    route.side_effect = [httpx.Response(400, json={"error": {"message": _DEGRADED_MSG}})]

    response = client.post(
        "/v1/messages",
        json={
            "model": "claude-3-5-sonnet-20241022",  # → DEFAULT_MODEL
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Hi"}],
        },
    )

    assert response.status_code == 400
    data = response.json()
    assert data["type"] == "error"
    # No pointless retry, and the message must be human-readable (not the raw UUID blob).
    assert len(route.calls) == 1
    assert "temporarily unavailable" in data["error"]["message"].lower()


def test_degraded_model_auto_falls_back_streaming(client, respx_mock, monkeypatch):
    monkeypatch.setitem(proxy.MODEL_ALIASES, "claude-opus-4-1", "deepseek-ai/deepseek-v4-pro")
    monkeypatch.setattr(proxy, "DEFAULT_MODEL", "nvidia/llama-3.3-nemotron-super-49b-v1.5")

    ok_stream = "".join(
        [
            "data: "
            + json.dumps({"choices": [{"delta": {"role": "assistant", "content": "hi"}}]})
            + "\n\n",
            "data: "
            + json.dumps(
                {
                    "choices": [{"finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
                }
            )
            + "\n\n",
            "data: [DONE]\n\n",
        ]
    )
    route = respx_mock.post("https://integrate.api.nvidia.com/v1/chat/completions")
    route.side_effect = [
        httpx.Response(400, json={"error": {"message": _DEGRADED_MSG}}),
        httpx.Response(200, content=ok_stream),
    ]

    with client.stream(
        "POST",
        "/v1/messages",
        json={
            "model": "claude-opus-4-1",
            "max_tokens": 100,
            "stream": True,
            "messages": [{"role": "user", "content": "Hi"}],
        },
    ) as response:
        assert response.status_code == 200
        events = [json.loads(l[6:]) for l in response.iter_lines() if l.startswith("data: ")]

    # Client should see a normal completion, never the DEGRADED error event.
    assert any(e["type"] == "message_stop" for e in events)
    assert not any(e["type"] == "error" for e in events)
    text = "".join(
        e["delta"]["text"]
        for e in events
        if e["type"] == "content_block_delta" and e["delta"].get("type") == "text_delta"
    )
    assert text == "hi"
    assert len(route.calls) == 2
    retry_payload = json.loads(route.calls[1].request.content)
    assert retry_payload["model"] == "nvidia/llama-3.3-nemotron-super-49b-v1.5"
