import json

import proxy


def test_resolve_model_maps_claude_alias_to_nvidia_default():
    assert proxy.resolve_model("claude-3-5-sonnet-20241022").startswith("nvidia/")


def test_resolve_model_passes_native_nvidia_model_through():
    model = "meta/llama-3.3-70b-instruct"
    assert proxy.resolve_model(model) == model


def test_translate_request_maps_model_and_system_and_tool():
    tool_id_map = {}
    body = {
        "model": "claude-3-5-sonnet-20241022",
        "system": [{"type": "text", "text": "You are useful."}],
        "max_tokens": 100,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "name": "read_file",
                "description": "Read a file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            },
            {"type": "web_search_20250305", "name": "web_search"},
        ],
        "tool_choice": {"type": "tool", "name": "read_file"},
        "metadata": {"user_id": "u1"},
    }
    out = proxy.translate_request(body, tool_id_map)
    assert out["model"] != body["model"]
    assert "Tool Use Protocol (STRICT)" in out["messages"][0]["content"]
    assert "You are useful." in out["messages"][0]["content"]
    assert out["messages"][1] == {"role": "user", "content": "hi"}
    assert out["tools"][0]["function"]["name"] == "read_file"
    assert len(out["tools"]) == 1
    assert out["tool_choice"] == {"type": "function", "function": {"name": "read_file"}}
    assert out["user"] == "u1"


def test_translate_tool_result_message_to_openai_tool_message():
    tool_id_map = {"toolu_abc": "call_abc"}
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "result:"},
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_abc",
                    "content": [{"type": "text", "text": "42"}],
                },
            ],
        }
    ]
    out = proxy.translate_messages(messages, tool_id_map)
    assert out[0] == {"role": "user", "content": "result:"}
    assert out[1] == {"role": "tool", "tool_call_id": "call_abc", "content": "42"}


def test_translate_response_tool_call_to_anthropic_tool_use():
    tool_id_map = {}
    oai = {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": json.dumps({"path": "README.md"}),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 4},
    }
    out = proxy.translate_response(oai, "nvidia/model", tool_id_map)
    block = out["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "read_file"
    assert block["input"] == {"path": "README.md"}
    assert out["stop_reason"] == "tool_use"


def test_error_type_mapping():
    assert proxy.map_error_type(401) == "authentication_error"
    assert proxy.map_error_type(429) == "rate_limit_error"
    assert proxy.map_error_type(529) == "overloaded_error"
    assert proxy.map_error_type(418) == "api_error"


def test_nvidia_error_message_nested_envelope():
    # NVIDIA wraps errors: {"error": {"message": "...", "type": "BadRequestError"}}
    body = {"error": {"message": "context length exceeded", "type": "BadRequestError"}}
    assert proxy._nvidia_error_message(body) == "context length exceeded"


def test_nvidia_error_message_top_level_fallback():
    # Some gateways return top-level "message"
    body = {"message": "rate limit hit"}
    assert proxy._nvidia_error_message(body) == "rate limit hit"


def test_nvidia_error_message_raw_string():
    assert proxy._nvidia_error_message("plain error text") == "plain error text"


def test_nvidia_error_message_json_string():
    # JSON embedded in a string (streaming error path)
    raw = '{"error": {"message": "token limit exceeded", "type": "BadRequestError"}}'
    assert proxy._nvidia_error_message(raw) == "token limit exceeded"


def test_context_safe_max_tokens_parses_nvidia_overflow():
    # Real NVIDIA context-overflow message format
    msg = (
        "You passed 186369 input tokens and requested 16384 output tokens. "
        "However, the model's context length is only 202752 tokens, resulting "
        "in a maximum input length of 186368 tokens."
    )
    safe = proxy._context_safe_max_tokens(msg)
    assert safe == 202752 - 186369 - proxy.CONTEXT_SAFETY_MARGIN


def test_context_safe_max_tokens_parses_multiline_prompt_overflow():
    msg = (
        "API Error: 400 This model's maximum context length is 131072 tokens.\n"
        "However, you requested 16382 output tokens and your prompt contains at\n"
        "least 114691 input tokens."
    )
    assert (
        proxy._context_safe_max_tokens(msg)
        == 131072 - 114691 - proxy.CONTEXT_SAFETY_MARGIN
    )


def test_context_safe_max_tokens_returns_none_for_unrelated_error():
    assert proxy._context_safe_max_tokens("rate limit exceeded") is None


def test_context_safe_max_tokens_returns_none_when_no_room():
    # input_tokens == context_window → no safe max_tokens
    msg = (
        "You passed 202752 input tokens and requested 1 output tokens. "
        "However, the model's context length is only 202752 tokens."
    )
    assert proxy._context_safe_max_tokens(msg) is None


def test_detect_stop_sequence_returns_match_for_visible_suffix():
    """When the model keeps a custom stop_sequence in the visible text,
    we should attribute end-of-turn to that sequence and echo it back."""
    assert proxy._detect_stop_sequence("Hello END", ["END"]) == "END"
    # Multi-sequence: the *latest* suffix wins (matches the order the model
    # emitted in, which is the only ordering Anthropic cares about).
    assert proxy._detect_stop_sequence("xyz###", ["###", "##"]) == "###"


def test_detect_stop_sequence_is_strict_suffix_only():
    """An internal substring (not at the end of the text) must not match -
    a false positive here would branch Claude Code on a bogus stop_reason."""
    assert proxy._detect_stop_sequence("Hello END world", ["END"]) is None
    # Empty sequences, empty text, and none passed: always no match.
    assert proxy._detect_stop_sequence("Hello", []) is None
    assert proxy._detect_stop_sequence("", ["x"]) is None
    assert proxy._detect_stop_sequence("Hello", [""]) is None
    assert proxy._detect_stop_sequence("Hello", None) is None


def test_lazy_key_returns_503_from_messages(monkeypatch):
    """With no NVIDIA_API_KEY, /v1/messages must return a clean 503 with a
    hint about how to set the secret — never a 500 AttributeError from the
    upstream client being None."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(proxy, "NVIDIA_API_KEY", "")
    with TestClient(proxy.app, raise_server_exceptions=False) as c:
        r = c.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 16,
            },
        )
    assert r.status_code == 503
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"
    # Actionable hint, not a bare "key missing".
    assert "NVIDIA_API_KEY" in body["error"]["message"]


def test_lazy_key_returns_503_from_count_tokens(monkeypatch):
    """Same no-key contract applies to /v1/messages/count_tokens."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(proxy, "NVIDIA_API_KEY", "")
    with TestClient(proxy.app, raise_server_exceptions=False) as c:
        r = c.post(
            "/v1/messages/count_tokens",
            json={
                "model": "claude-3-5-sonnet-20241022",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
    assert r.status_code == 503
    assert r.json()["error"]["type"] == "authentication_error"


def test_anthropic_usage_separates_cached_from_prompt():
    """Claude Code credits ``cache_read_input_tokens`` separately from
    ``input_tokens`` (which represents *non-cached* prompt tokens). The
    two must not double-count. A cached count larger than the prompt
    itself (degenerate upstream) must clamp at zero, never go negative."""
    usage = {
        "prompt_tokens": 100,
        "completion_tokens": 5,
        "prompt_tokens_details": {"cached_tokens": 80},
    }
    out = proxy._anthropic_usage(usage)
    assert out["input_tokens"] == 20
    assert out["output_tokens"] == 5
    assert out["cache_read_input_tokens"] == 80
    assert out["cache_creation_input_tokens"] == 0

    # Degenerate: cached > prompt shouldn't underflow.
    weird = {
        "prompt_tokens": 10,
        "completion_tokens": 1,
        "prompt_tokens_details": {"cached_tokens": 999},
    }
    out2 = proxy._anthropic_usage(weird)
    assert out2["input_tokens"] == 0
    assert out2["cache_read_input_tokens"] == 999

    # No details block → zero cached, prompt reported as-is.
    out3 = proxy._anthropic_usage({"prompt_tokens": 7, "completion_tokens": 2})
    assert out3["input_tokens"] == 7
    assert out3["cache_read_input_tokens"] == 0
