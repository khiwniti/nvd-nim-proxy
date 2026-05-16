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
