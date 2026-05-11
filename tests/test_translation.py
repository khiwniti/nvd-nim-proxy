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
                "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
            {"type": "web_search_20250305", "name": "web_search"},
        ],
        "tool_choice": {"type": "tool", "name": "read_file"},
        "metadata": {"user_id": "u1"},
    }
    out = proxy.translate_request(body, tool_id_map)
    assert out["model"] != body["model"]
    assert out["messages"][0] == {"role": "system", "content": "You are useful."}
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
                {"type": "tool_result", "tool_use_id": "toolu_abc", "content": [{"type": "text", "text": "42"}]},
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
                            "function": {"name": "read_file", "arguments": json.dumps({"path": "README.md"})},
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
