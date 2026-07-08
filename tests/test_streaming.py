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


def test_stop_sequence_promoted_in_finalize_when_suffix_matches():
    """Client-supplied stop_sequences: when upstream ends with the sequence
    on the visible text, finalize() must upgrade end_turn → stop_sequence
    and echo the matching sequence."""
    st = proxy.StreamTranslator(
        "nvidia/model", {}, stop_sequences=["END_OF_TURN"]
    )
    events = list(
        st.feed(
            {"choices": [{"delta": {"content": "Hello END_OF_TURN"}, "finish_reason": "stop"}]}
        )
    )
    events += list(st.finalize())
    md = [e for e in events if e["event"] == "message_delta"]
    assert md, "message_delta missing"
    assert md[-1]["data"]["delta"]["stop_reason"] == "stop_sequence"
    assert md[-1]["data"]["delta"]["stop_sequence"] == "END_OF_TURN"


def test_stop_sequence_not_promoted_when_text_does_not_end_with_it():
    """If the model produces text without the trailing stop sequence, we
    MUST keep end_turn. False positives are worse than misses."""
    st = proxy.StreamTranslator(
        "nvidia/model", {}, stop_sequences=["END_OF_TURN"]
    )
    events = list(
        st.feed(
            {"choices": [{"delta": {"content": "Hello world"}, "finish_reason": "stop"}]}
        )
    )
    events += list(st.finalize())
    md = [e for e in events if e["event"] == "message_delta"]
    assert md[-1]["data"]["delta"]["stop_reason"] == "end_turn"
    assert md[-1]["data"]["delta"]["stop_sequence"] is None


def test_message_stop_emitted_even_after_translation_error_in_feed():
    """Forced upstream bug shouldn't truncate the SSE — feed() may throw; the
    outer stream_response must still emit message_stop. We assert the contract
    at the StreamTranslator level: finalize() runs cleanly regardless of what
    preceded it."""
    st = proxy.StreamTranslator("nvidia/model", {})
    # Force a tool block to be marked started but never receive a finish:
    broken = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_broken",
                            "function": {
                                "name": "do_thing",
                                # args truncated at the quote — the validator in
                                # finalize() should still produce parseable JSON.
                                "arguments": '{"key": "val',
                            },
                        }
                    ]
                }
            }
        ]
    }
    list(st.feed(broken))
    events = list(st.finalize())
    names = [e["event"] for e in events]
    assert names[-1] == "message_stop"
    # Tool block must have its own stop (drain pass).
    assert names.count("content_block_stop") >= 1


def test_inline_think_unclosed_at_finish_becomes_thinking_block():
    """If ``<think>`` opens but never closes before finish_reason, the buffered
    prefix must still surface as a thinking block rather than silently dropped."""
    st = proxy.StreamTranslator("nvidia/model", {})
    events = list(
        st.feed(
            {
                "choices": [
                    {
                        "delta": {"content": "before <think>reasoning without"},
                        "finish_reason": None,
                    }
                ]
            }
        )
    )
    events += list(
        st.feed(
            {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        )
    )
    events += list(st.finalize())
    starts = [
        e["data"]["content_block"]["type"]
        for e in events
        if e["event"] == "content_block_start"
    ]
    assert "text" in starts
    assert "thinking" in starts
    assert events[-1]["event"] == "message_stop"
