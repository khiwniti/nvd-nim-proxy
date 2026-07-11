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


def test_parallel_tool_calls_emit_distinct_blocks():
    """Anthropic SSE requires one tool_use block per call, each closed.

    OpenAI/NVIDIA may emit multiple ``tool_calls`` indices in the *same*
    delta. Both must surface as distinct Anthropic ``tool_use`` blocks rather
    than collapsing into a single block with merged JSON arguments. This
    regression test guards the sequential close-on-open path that prevents
    the second tool from harvesting the first tool's buffered args on its
    own index."""
    st = proxy.StreamTranslator(
        "nvidia/model",
        {"call_a": "toolu_aaa", "call_b": "toolu_bbb"},
    )
    chunks = [
        # First delta announces both tools.
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_a",
                                "function": {"name": "do_a", "arguments": ""},
                            },
                            {
                                "index": 1,
                                "id": "call_b",
                                "function": {"name": "do_b", "arguments": ""},
                            },
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        # Second delta fills in both arguments in one shot.
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": '{"x":1}'},
                            },
                            {
                                "index": 1,
                                "function": {"arguments": '{"y":2}'},
                            },
                        ]
                    },
                    "finish_reason": None,
                }
            ]
        },
        # Stream terminates after the tools are emitted.
        {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    ]
    events: list = []
    for chunk in chunks:
        events += list(st.feed(chunk))
    events += list(st.finalize())

    starts = [
        e["data"]["content_block"]
        for e in events
        if e["event"] == "content_block_start"
        and e["data"]["content_block"]["type"] == "tool_use"
    ]
    stops = [
        e["data"]["index"]
        for e in events
        if e["event"] == "content_block_stop"
    ]
    assert len(starts) == 2, f"expected two tool_use blocks, got {len(starts)}"
    assert len(stops) == 2, f"expected two content_block_stops, got {len(stops)}"

    # Each tool_use carries its own Anthropic id distinct from the others.
    assert starts[0]["id"] != starts[1]["id"]
    # Each tool's input_json_delta points to the *same* index its
    # content_block_start used, and parcels out exactly one of the two
    # argument strings verbatim.
    json_deltas = {
        e["data"]["index"]: e["data"]["delta"]["partial_json"]
        for e in events
        if e["event"] == "content_block_delta"
        and e["data"]["delta"]["type"] == "input_json_delta"
    }
    assert set(json_deltas) == set(stops), (
        "every tool index must receive exactly one validated input_json_delta"
    )
    # JSON parse → dictionary equality, regardless of whitespace. We
    # normalise via a sorted (key, repr) tuple so {"x":1} vs {"y":2}
    # compare deterministically without dict ordering artefacts.
    import json as _json

    parsed = {_json.dumps(d, sort_keys=True): d for d in (
        _json.loads(v) for v in json_deltas.values()
    )}
    assert parsed == {'{"x": 1}': {"x": 1}, '{"y": 2}': {"y": 2}}


def test_close_open_drain_emits_stop_for_abandoned_tool_buffer():
    """If upstream opens a tool block but the stream drops before finish,
    finalize() must still emit a matching content_block_stop. Otherwise
    Claude Code hangs on a never-closing block ('spinner never stops')."""
    st = proxy.StreamTranslator(
        "nvidia/model", {"call_a": "toolu_aaa"}
    )
    feed_events = list(
        st.feed(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_a",
                                    "function": {
                                        "name": "do_a",
                                        "arguments": '{"x":1}',
                                    },
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        )
    )
    # No finish_reason, no second call — upstream just drops the stream.
    finalize_events = list(st.finalize())
    all_events = feed_events + finalize_events
    starts = [
        e["data"]["index"]
        for e in all_events
        if e["event"] == "content_block_start"
        and e["data"]["content_block"]["type"] == "tool_use"
    ]
    stops = [
        e["data"]["index"]
        for e in all_events
        if e["event"] == "content_block_stop"
    ]
    assert starts, "tool block should have been opened"
    assert len(stops) >= 1, (
        "every opened tool index must have a matching stop event; "
        f"have starts={starts}, no stops present"
    )
    assert starts[0] in stops, (
        "the opened tool's index must be among the stops the drain pass emits"
    )
    # And message_stop is still the final bookend.
    assert all_events[-1]["event"] == "message_stop"


def test_usage_block_echoes_cached_tokens():
    """Anthropic's UI credits cached tokens separately; we must echo the
    upstream ``prompt_tokens_details.cached_tokens`` count instead of
    silently zeroing it (which would mean the user never sees cache hit
    savings in their session accounting)."""
    st = proxy.StreamTranslator("nvidia/model", {})
    list(
        st.feed(
            {
                "choices": [
                    {"delta": {"content": "ok"}, "finish_reason": "stop"}
                ]
            }
        )
    )
    list(
        st.feed(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
            }
        )
    )
    events = list(st.finalize())
    md = [e for e in events if e["event"] == "message_delta"][-1]
    usage = md["data"]["usage"]
    # input_tokens = non-cached portion only, cached portion reported
    # separately so the two don't double-count.
    assert usage["input_tokens"] == 20
    assert usage["output_tokens"] == 5
    assert usage["cache_read_input_tokens"] == 80
    assert usage["cache_creation_input_tokens"] == 0
