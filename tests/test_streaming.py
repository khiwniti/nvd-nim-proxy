import proxy


def event_names(events):
    return [e["event"] for e in events]


def test_stream_translator_text_event_order():
    st = proxy.StreamTranslator("nvidia/model", {})
    chunk = {"choices": [{"delta": {"content": "hello world"}, "finish_reason": None}]}
    events = list(st.feed(chunk))
    events += list(st.feed({"choices": [{"delta": {}, "finish_reason": "stop"}]}))
    events += list(st.finalize())
    names = event_names(events)
    assert names[:2] == ["content_block_start", "content_block_delta"]
    assert "content_block_stop" in names
    assert names[-2:] == ["message_delta", "message_stop"]


def test_stream_translator_inline_thinking_tags():
    st = proxy.StreamTranslator("nvidia/model", {})
    events = list(st.feed({"choices": [{"delta": {"content": "<think>plan</think>answer"}, "finish_reason": "stop"}]}))
    events += list(st.finalize())
    starts = [e["data"]["content_block"]["type"] for e in events if e["event"] == "content_block_start"]
    assert "thinking" in starts
    assert "text" in starts


def test_stream_translator_tool_call_delta():
    st = proxy.StreamTranslator("nvidia/model", {})
    events = list(st.feed({
        "choices": [{
            "delta": {
                "tool_calls": [{
                    "index": 0,
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": '{"path":"README.md"}'},
                }]
            },
            "finish_reason": "tool_calls",
        }]
    }))
    events += list(st.finalize())
    assert any(e["event"] == "content_block_start" and e["data"]["content_block"]["type"] == "tool_use" for e in events)
    assert any(e["event"] == "content_block_delta" and e["data"]["delta"]["type"] == "input_json_delta" for e in events)


def test_encode_sse_contains_event_and_json_data():
    raw = proxy.encode_sse("ping", {"type": "ping"})
    assert raw.startswith(b"event: ping\n")
    assert b'data: {"type":"ping"}' in raw
