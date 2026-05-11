#!/usr/bin/env python3
"""
nvd-claude-proxy — Anthropic Messages API → NVIDIA NIM hosted catalog.

Single-file proxy that lets Claude Code use https://integrate.api.nvidia.com.
No model registry, no schema layer, no production hardening ceremony.
Just enough to make Claude Code feel responsive.

Required env:
    NVIDIA_API_KEY       Bearer key from https://build.nvidia.com

Optional env:
    PROXY_HOST           default 127.0.0.1
    PROXY_PORT           default 8787
    NVIDIA_BASE_URL      default https://integrate.api.nvidia.com/v1
    PROXY_API_KEY        if set, clients must present it as x-api-key
    LOG_LEVEL            default info

Run:
    pip install -r requirements.txt
    NVIDIA_API_KEY=nvapi-... python proxy.py

Then in another shell, point Claude Code at it:
    M=nvidia/llama-3.3-nemotron-super-49b-v1.5
    export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
    export ANTHROPIC_API_KEY=not-used
    export ANTHROPIC_CUSTOM_MODEL_OPTION=$M
    export ANTHROPIC_DEFAULT_HAIKU_MODEL=$M
    export ANTHROPIC_DEFAULT_OPUS_MODEL=$M
    export ANTHROPIC_DEFAULT_SONNET_MODEL=$M
    export CLAUDE_CODE_SUBAGENT_MODEL=$M
    claude
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
import orjson
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import ORJSONResponse, StreamingResponse


# ─── Config ──────────────────────────────────────────────────────────────────

NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY")
NVIDIA_BASE_URL = os.environ.get(
    "NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"
)
PROXY_HOST = os.environ.get("PROXY_HOST", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_PORT", "8787"))
PROXY_API_KEY = os.environ.get("PROXY_API_KEY") or None
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info")

# 15 s matches Anthropic's official cadence; keeps Claude Code's TUI alive
# during long reasoning phases (Nemotron Ultra can think for 30+ seconds).
PING_INTERVAL = 15.0

# Anthropic streams at ~sub-word granularity. NVIDIA emits 10–40-char chunks.
# We resplit on word/punctuation boundaries to recreate the "typing" feel.
TEXT_DELTA_CHARS = 6


# ─── Lifespan: one HTTP client for the whole process ─────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not NVIDIA_API_KEY:
        raise RuntimeError("NVIDIA_API_KEY env var is required")
    try:
        import h2  # noqa: F401
        http2 = True
    except ImportError:
        http2 = False
    app.state.nvidia = httpx.AsyncClient(
        base_url=NVIDIA_BASE_URL,
        http2=http2,
        timeout=httpx.Timeout(connect=10.0, read=600.0, write=30.0, pool=10.0),
        limits=httpx.Limits(
            max_connections=100, max_keepalive_connections=20, keepalive_expiry=60.0
        ),
        headers={
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
            "Accept": "application/json",
            "User-Agent": f"nvd-claude-proxy/1.0 (h2={int(http2)})",
        },
    )
    print(f"nvd-claude-proxy ready (HTTP/2={http2}) → {NVIDIA_BASE_URL}")
    yield
    await app.state.nvidia.aclose()


app = FastAPI(
    title="nvd-claude-proxy",
    version="1.0.0",
    lifespan=lifespan,
    default_response_class=ORJSONResponse,
)


# ─── ID helpers ──────────────────────────────────────────────────────────────

def new_msg_id() -> str:
    return "msg_" + secrets.token_urlsafe(18)


def new_tool_id() -> str:
    return "toolu_" + secrets.token_urlsafe(18)


def new_signature() -> str:
    """Opaque proxy-local signature. Will not validate against the real
    Anthropic API — round-tripping thinking blocks elsewhere is unsupported."""
    return "proxy-" + secrets.token_urlsafe(24)


# ─── Anthropic → OpenAI request translation ──────────────────────────────────

# Pattern matches Anthropic's server-tool type IDs (web_search_20250305,
# computer_20250124, bash_20250124, code_execution_20260120, memory_20250818, …).
# These have no NVIDIA equivalent; we drop them silently.
SERVER_TOOL_RE = re.compile(r"_20\d{6}$")


def flatten_system(system) -> str | None:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    parts = [b.get("text", "") for b in system if b.get("type") == "text"]
    return "\n\n".join(parts) or None


def image_block_to_openai(block: dict) -> dict:
    src = block.get("source") or {}
    t = src.get("type")
    if t == "url":
        return {"type": "image_url", "image_url": {"url": src["url"]}}
    if t == "base64":
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"},
        }
    raise ValueError(f"unsupported image source type: {t}")


def translate_messages(
    messages: list[dict], tool_id_map: dict[str, str]
) -> list[dict]:
    """Anthropic message list → OpenAI message list.

    Each Anthropic user message that contains tool_result blocks explodes into
    one OpenAI user message + one role:"tool" message per result. The
    tool_id_map preserves Anthropic toolu_… ids verbatim as OpenAI call ids
    so round-trips work without lookup contortions.
    """
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        text_parts: list[dict] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []

        for block in content or []:
            btype = block.get("type")
            if btype == "text":
                text_parts.append({"type": "text", "text": block["text"]})
            elif btype == "image":
                text_parts.append(image_block_to_openai(block))
            elif btype == "tool_use":
                tool_id_map[block["id"]] = block["id"]
                tool_calls.append({
                    "id": block["id"],
                    "type": "function",
                    "function": {
                        "name": block["name"],
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                })
            elif btype == "tool_result":
                tid = block["tool_use_id"]
                openai_id = tool_id_map.get(tid, tid)
                raw = block.get("content", "")
                if isinstance(raw, list):
                    raw = "".join(
                        b.get("text", "") for b in raw if b.get("type") == "text"
                    )
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": openai_id,
                    "content": str(raw) if raw is not None else "",
                })
            # thinking / redacted_thinking / document blocks: drop silently.
            # NVIDIA cannot consume opaque Anthropic signatures, and PDFs
            # would need server-side text extraction (out of MVP scope).

        if role == "assistant":
            msg: dict = {"role": "assistant"}
            if text_parts:
                msg["content"] = (
                    "".join(p["text"] for p in text_parts)
                    if all(p["type"] == "text" for p in text_parts)
                    else text_parts
                )
            else:
                msg["content"] = None
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
        else:  # user
            if text_parts:
                out.append({
                    "role": "user",
                    "content": (
                        "".join(p["text"] for p in text_parts)
                        if all(p["type"] == "text" for p in text_parts)
                        else text_parts
                    ),
                })
            out.extend(tool_results)
    return out


def translate_tools(tools: list[dict] | None) -> list[dict] | None:
    if not tools:
        return None
    out = []
    for t in tools:
        # Skip Anthropic server-side tools — no NVIDIA equivalent
        if SERVER_TOOL_RE.search(t.get("type") or ""):
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description") or "",
                "parameters": t.get("input_schema") or {
                    "type": "object", "properties": {}
                },
            },
        })
    return out or None


def translate_tool_choice(tc):
    if tc is None:
        return None
    if isinstance(tc, str):
        return tc
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "none":
        return "none"
    if t == "tool":
        return {"type": "function", "function": {"name": tc["name"]}}
    return "auto"


def translate_request(body: dict, tool_id_map: dict[str, str]) -> dict:
    msgs: list[dict] = []
    if (sys := flatten_system(body.get("system"))):
        msgs.append({"role": "system", "content": sys})
    msgs.extend(translate_messages(body.get("messages") or [], tool_id_map))

    payload: dict = {
        "model": body["model"],
        "messages": msgs,
        "max_tokens": body.get("max_tokens", 4096),
        "stream": bool(body.get("stream", False)),
    }
    for key in ("temperature", "top_p", "top_k"):
        if (v := body.get(key)) is not None:
            payload[key] = v
    if (ss := body.get("stop_sequences")) is not None:
        payload["stop"] = ss
    if (tools := translate_tools(body.get("tools"))):
        payload["tools"] = tools
        if (tc := translate_tool_choice(body.get("tool_choice"))) is not None:
            payload["tool_choice"] = tc
    # Forward Claude Code's metadata.user_id as OpenAI's `user`
    if (md := body.get("metadata")) and isinstance(md, dict):
        if (uid := md.get("user_id")):
            payload["user"] = str(uid)
    return payload


# ─── Non-streaming response translation ──────────────────────────────────────

THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)
FINISH_TO_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
    None: "end_turn",
}


def extract_thinking(content: str | None, reasoning: str | None) -> tuple[str | None, str]:
    """Return (thinking_text, remaining_content). Handles both surfaces:
    a separate `reasoning_content` field, or inline <think>…</think> tags."""
    if reasoning:
        return reasoning, content or ""
    if content and "<think>" in content:
        if (m := THINK_RE.search(content)):
            return m.group(1).strip(), THINK_RE.sub("", content, count=1).lstrip()
    return None, content or ""


def translate_response(oai: dict, model: str, tool_id_map: dict[str, str]) -> dict:
    choice = (oai.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks: list[dict] = []

    thinking, remaining = extract_thinking(
        msg.get("content"), msg.get("reasoning_content")
    )
    if thinking:
        blocks.append({
            "type": "thinking",
            "thinking": thinking,
            "signature": new_signature(),
        })
    if remaining:
        blocks.append({"type": "text", "text": remaining})

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            args = {"_raw_arguments": fn.get("arguments", "")}
        oid = tc.get("id") or new_tool_id()
        anth_id = oid if oid.startswith("toolu_") else new_tool_id()
        tool_id_map[anth_id] = oid
        blocks.append({
            "type": "tool_use",
            "id": anth_id,
            "name": fn.get("name", ""),
            "input": args,
        })

    usage = oai.get("usage") or {}
    return {
        "id": new_msg_id(),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": FINISH_TO_STOP.get(choice.get("finish_reason"), "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


# ─── Streaming state machine ─────────────────────────────────────────────────

_SPLIT_RE = re.compile(r"(\s+|[.,;:!?)\]\}]|[(\[\{])")


def retokenize(s: str, max_chars: int = TEXT_DELTA_CHARS) -> list[str]:
    """Split s into ≤max_chars deltas at word/punctuation boundaries.
    Pure cosmetic — recreates the Anthropic 'typing' feel from coarse
    NVIDIA chunks."""
    if len(s) <= max_chars:
        return [s]
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        end = min(i + max_chars, n)
        if end < n:
            ms = list(_SPLIT_RE.finditer(s[i:end]))
            if ms:
                end = i + ms[-1].end()
        out.append(s[i:end])
        i = end
    return out


_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"


def _safe_suffix_len(buf: str, target: str) -> int:
    """If buf ends with a prefix of target, return that prefix's length."""
    for k in range(min(len(buf), len(target) - 1), 0, -1):
        if buf.endswith(target[:k]):
            return k
    return 0


class StreamTranslator:
    """OpenAI streaming chunks → Anthropic SSE events.

    Tracks a single 'currently open' content block at a time. Switching
    modalities (text↔thinking↔tool_use) closes the previous block first.
    """

    def __init__(self, model: str, tool_id_map: dict[str, str]):
        self.model = model
        self.tool_id_map = tool_id_map
        self.next_index = 0
        self.open_type: str | None = None  # "text" | "thinking" | "tool_use"
        self.open_index: int | None = None
        self.signature_sent = False
        # Inline <think>…</think> tag handling (when reasoning_content not used)
        self.in_inline_think = False
        self.text_buf = ""
        # Per-OpenAI-tool-call-index buffers
        self.tools: dict[int, dict] = {}
        self.stop_reason = "end_turn"
        self.usage_in = 0
        self.usage_out = 0

    @staticmethod
    def _ev(event: str, data: dict) -> dict:
        return {"event": event, "data": data}

    def _close_open(self):
        if self.open_index is None:
            return
        if self.open_type == "thinking" and not self.signature_sent:
            yield self._ev("content_block_delta", {
                "type": "content_block_delta",
                "index": self.open_index,
                "delta": {
                    "type": "signature_delta",
                    "signature": new_signature(),
                },
            })
            self.signature_sent = True
        yield self._ev("content_block_stop", {
            "type": "content_block_stop", "index": self.open_index,
        })
        self.open_type = None
        self.open_index = None
        self.signature_sent = False

    def _open_text(self):
        idx = self.next_index
        self.next_index += 1
        self.open_type = "text"
        self.open_index = idx
        yield self._ev("content_block_start", {
            "type": "content_block_start",
            "index": idx,
            "content_block": {"type": "text", "text": ""},
        })

    def _open_thinking(self):
        idx = self.next_index
        self.next_index += 1
        self.open_type = "thinking"
        self.open_index = idx
        self.signature_sent = False
        yield self._ev("content_block_start", {
            "type": "content_block_start",
            "index": idx,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        })

    def _emit_text(self, s: str):
        if not s:
            return
        if self.open_type != "text":
            yield from self._close_open()
            yield from self._open_text()
        for piece in retokenize(s):
            yield self._ev("content_block_delta", {
                "type": "content_block_delta",
                "index": self.open_index,
                "delta": {"type": "text_delta", "text": piece},
            })

    def _emit_thinking(self, s: str):
        if not s:
            return
        if self.open_type != "thinking":
            yield from self._close_open()
            yield from self._open_thinking()
        for piece in retokenize(s):
            yield self._ev("content_block_delta", {
                "type": "content_block_delta",
                "index": self.open_index,
                "delta": {"type": "thinking_delta", "thinking": piece},
            })

    def _process_text(self, s: str):
        """Watch for <think>…</think> tags that may straddle chunk boundaries."""
        buf = self.text_buf + s
        self.text_buf = ""
        while buf:
            if self.in_inline_think:
                idx = buf.find(_THINK_CLOSE)
                if idx == -1:
                    suf = _safe_suffix_len(buf, _THINK_CLOSE)
                    if suf:
                        yield from self._emit_thinking(buf[:-suf])
                        self.text_buf = buf[-suf:]
                    else:
                        yield from self._emit_thinking(buf)
                    return
                yield from self._emit_thinking(buf[:idx])
                yield from self._close_open()
                self.in_inline_think = False
                buf = buf[idx + len(_THINK_CLOSE):]
            else:
                idx = buf.find(_THINK_OPEN)
                if idx == -1:
                    suf = _safe_suffix_len(buf, _THINK_OPEN)
                    if suf:
                        yield from self._emit_text(buf[:-suf])
                        self.text_buf = buf[-suf:]
                    else:
                        yield from self._emit_text(buf)
                    return
                yield from self._emit_text(buf[:idx])
                yield from self._close_open()
                yield from self._open_thinking()
                self.in_inline_think = True
                buf = buf[idx + len(_THINK_OPEN):]

    def feed(self, chunk: dict):
        # Trailing usage-only chunk (choices == [])
        if not chunk.get("choices"):
            if (u := chunk.get("usage")):
                self.usage_in = u.get("prompt_tokens", self.usage_in)
                self.usage_out = u.get("completion_tokens", self.usage_out)
            return

        choice = chunk["choices"][0]
        delta = choice.get("delta") or {}
        finish = choice.get("finish_reason")

        if (rc := delta.get("reasoning_content")):
            yield from self._emit_thinking(rc)
        if (text := delta.get("content")):
            yield from self._process_text(text)

        for tc in delta.get("tool_calls") or []:
            o_idx = tc.get("index", 0)
            buf = self.tools.setdefault(o_idx, {
                "oid": None, "name": None, "started": False,
                "anth_id": None, "anth_idx": None,
            })
            fn = tc.get("function") or {}
            if (i := tc.get("id")):
                buf["oid"] = i
            if (n := fn.get("name")):
                buf["name"] = n
            args = fn.get("arguments")

            if not buf["started"] and buf["oid"] and buf["name"]:
                yield from self._close_open()
                anth_id = (
                    buf["oid"] if buf["oid"].startswith("toolu_") else new_tool_id()
                )
                self.tool_id_map[anth_id] = buf["oid"]
                buf["anth_id"] = anth_id
                buf["anth_idx"] = self.next_index
                self.next_index += 1
                self.open_type = "tool_use"
                self.open_index = buf["anth_idx"]
                buf["started"] = True
                yield self._ev("content_block_start", {
                    "type": "content_block_start",
                    "index": buf["anth_idx"],
                    "content_block": {
                        "type": "tool_use",
                        "id": anth_id,
                        "name": buf["name"],
                        "input": {},
                    },
                })
            if buf["started"] and args:
                yield self._ev("content_block_delta", {
                    "type": "content_block_delta",
                    "index": buf["anth_idx"],
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": args,
                    },
                })

        if finish:
            if self.text_buf:
                tail, self.text_buf = self.text_buf, ""
                yield from (
                    self._emit_thinking if self.in_inline_think else self._emit_text
                )(tail)
            yield from self._close_open()
            self.stop_reason = FINISH_TO_STOP.get(finish, "end_turn")

    def finalize(self):
        if self.text_buf:
            yield from (
                self._emit_thinking if self.in_inline_think else self._emit_text
            )(self.text_buf)
            self.text_buf = ""
        yield from self._close_open()
        yield self._ev("message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": self.stop_reason,
                "stop_sequence": None,
            },
            "usage": {
                "input_tokens": self.usage_in,
                "output_tokens": self.usage_out,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        })
        yield self._ev("message_stop", {"type": "message_stop"})


# ─── HTTP routes ─────────────────────────────────────────────────────────────

def encode_sse(event: str, data: dict) -> bytes:
    return b"event: " + event.encode() + b"\ndata: " + orjson.dumps(data) + b"\n\n"


STATUS_TO_ERR = {
    400: "invalid_request_error", 401: "authentication_error",
    403: "permission_error", 404: "not_found_error",
    413: "request_too_large", 422: "invalid_request_error",
    429: "rate_limit_error", 500: "api_error", 502: "api_error",
    503: "overloaded_error", 504: "api_error", 529: "overloaded_error",
}


def map_error_type(status: int) -> str:
    return STATUS_TO_ERR.get(status, "api_error")


def check_auth(request: Request):
    if not PROXY_API_KEY:
        return
    presented = (
        request.headers.get("x-api-key")
        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )
    if presented != PROXY_API_KEY:
        raise HTTPException(401, {
            "type": "error",
            "error": {"type": "authentication_error", "message": "invalid proxy api key"},
        })


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models(request: Request):
    """Proxy NVIDIA's /v1/models verbatim."""
    nvidia: httpx.AsyncClient = request.app.state.nvidia
    r = await nvidia.get("/models")
    return ORJSONResponse(r.json(), status_code=r.status_code)


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Heuristic token count — NVIDIA's hosted catalog has no native endpoint
    for this. ~4 chars/token holds within ±15% for Llama/Nemotron/Qwen."""
    check_auth(request)
    body = await request.json()

    def walk(o):
        if isinstance(o, dict):
            return sum(walk(v) for k, v in o.items() if k != "data")
        if isinstance(o, list):
            return sum(walk(v) for v in o)
        if isinstance(o, str):
            return len(o)
        return 0

    return ORJSONResponse({"input_tokens": max(walk(body) // 4, 1)})


@app.post("/v1/messages")
async def messages(request: Request):
    check_auth(request)
    try:
        body = await request.json()
    except Exception as e:
        return ORJSONResponse(
            {"type": "error",
             "error": {"type": "invalid_request_error", "message": f"bad JSON: {e}"}},
            status_code=400,
        )
    if "model" not in body or "messages" not in body:
        return ORJSONResponse(
            {"type": "error",
             "error": {"type": "invalid_request_error",
                       "message": "model and messages are required"}},
            status_code=400,
        )

    tool_id_map: dict[str, str] = {}
    payload = translate_request(body, tool_id_map)
    nvidia: httpx.AsyncClient = request.app.state.nvidia

    # Non-streaming path
    if not body.get("stream"):
        try:
            resp = await nvidia.post("/chat/completions", json=payload)
        except httpx.HTTPError as e:
            return ORJSONResponse(
                {"type": "error",
                 "error": {"type": "api_error", "message": str(e)[:500]}},
                status_code=502,
            )
        if resp.status_code >= 400:
            try:
                body_err = resp.json().get("detail") or resp.json().get("message") or resp.text
            except Exception:
                body_err = resp.text
            return ORJSONResponse(
                {"type": "error",
                 "error": {"type": map_error_type(resp.status_code),
                           "message": str(body_err)[:500]}},
                status_code=resp.status_code,
            )
        return ORJSONResponse(translate_response(resp.json(), body["model"], tool_id_map))

    # Streaming path
    return StreamingResponse(
        stream_response(request, body, payload, tool_id_map),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",         # defeat nginx/proxy buffering
            "Content-Encoding": "identity",    # never gzip an SSE stream
        },
    )


async def stream_response(
    request: Request, body: dict, payload: dict, tool_id_map: dict[str, str]
) -> AsyncIterator[bytes]:
    """The hot path. Three things to remember:
    1. Emit message_start IMMEDIATELY — don't wait for NVIDIA's first chunk.
    2. Ping every 15 s while idle so Claude Code's TUI never freezes.
    3. Cancel the upstream task on client disconnect.
    """
    nvidia: httpx.AsyncClient = request.app.state.nvidia
    model = body["model"]
    msg_id = new_msg_id()

    # 1) Eager message_start — synchronous yield before any await.
    yield encode_sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 0, "output_tokens": 0,
                "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            },
        },
    })

    st = StreamTranslator(model=model, tool_id_map=tool_id_map)
    payload = {
        **payload,
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    # 2) Producer/consumer: NVIDIA chunks flow into a queue; the main loop
    # interleaves them with periodic pings.
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    DONE = object()
    ERROR = object()

    async def producer():
        try:
            async with nvidia.stream(
                "POST", "/chat/completions", json=payload
            ) as r:
                if r.status_code >= 400:
                    body_bytes = await r.aread()
                    raise httpx.HTTPStatusError(
                        f"NVIDIA {r.status_code}: "
                        f"{body_bytes.decode(errors='replace')[:500]}",
                        request=r.request, response=r,
                    )
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].lstrip()
                    if data == "[DONE]":
                        break
                    try:
                        await queue.put(json.loads(data))
                    except json.JSONDecodeError:
                        continue
        except Exception as e:  # noqa: BLE001
            await queue.put((ERROR, e))
        finally:
            await queue.put(DONE)

    prod = asyncio.create_task(producer())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=PING_INTERVAL)
            except asyncio.TimeoutError:
                yield encode_sse("ping", {"type": "ping"})
                if await request.is_disconnected():
                    prod.cancel()
                    return
                continue
            if item is DONE:
                break
            if isinstance(item, tuple) and item and item[0] is ERROR:
                exc = item[1]
                status = getattr(getattr(exc, "response", None), "status_code", 500)
                yield encode_sse("error", {
                    "type": "error",
                    "error": {
                        "type": map_error_type(status),
                        "message": str(exc)[:500],
                    },
                })
                break
            for ev in st.feed(item):
                yield encode_sse(ev["event"], ev["data"])
        for ev in st.finalize():
            yield encode_sse(ev["event"], ev["data"])
    finally:
        if not prod.done():
            prod.cancel()
            try:
                await prod
            except (asyncio.CancelledError, Exception):
                pass


# ─── Entrypoint ──────────────────────────────────────────────────────────────

def main():
    if not NVIDIA_API_KEY:
        raise SystemExit("NVIDIA_API_KEY env var is required")

    # Prefer uvloop+httptools if available; fall back to asyncio default.
    kwargs = {"host": PROXY_HOST, "port": PROXY_PORT, "log_level": LOG_LEVEL,
              "access_log": False}
    try:
        import uvloop  # noqa: F401
        kwargs["loop"] = "uvloop"
    except ImportError:
        pass
    try:
        import httptools  # noqa: F401
        kwargs["http"] = "httptools"
    except ImportError:
        pass

    print(f"\nnvd-claude-proxy v1.0")
    print(f"  listen      → http://{PROXY_HOST}:{PROXY_PORT}")
    print(f"  upstream    → {NVIDIA_BASE_URL}")
    print(f"  point Claude Code's ANTHROPIC_BASE_URL at http://{PROXY_HOST}:{PROXY_PORT}\n")
    uvicorn.run(app, **kwargs)


if __name__ == "__main__":
    main()
