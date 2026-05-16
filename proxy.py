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
import hmac
import json
import os
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import orjson
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

# ─── Config ──────────────────────────────────────────────────────────────────

DEFAULT_NVIDIA_MODEL = "nvidia/llama-3.3-nemotron-super-49b-v1.5"
DEFAULT_MODEL_ALIASES = {
    # ── Standard Claude Code aliases ─────────────────────────────────────────
    # Mapped to meaningful NVIDIA equivalents by capability tier so that
    # switching models in Claude Desktop actually changes the backend.
    "claude-haiku-4-5": "meta/llama-3.1-8b-instruct",  # fast / cheap
    "claude-3-5-sonnet-20241022": DEFAULT_NVIDIA_MODEL,  # balanced
    "claude-3-7-sonnet-20250219": DEFAULT_NVIDIA_MODEL,
    "claude-sonnet-4-20250514": DEFAULT_NVIDIA_MODEL,
    "claude-sonnet-4-5": DEFAULT_NVIDIA_MODEL,
    "claude-opus-4-1": "nvidia/llama-3.1-nemotron-ultra-253b-v1",  # most capable
    # ── NVIDIA catalog exposed as claude-nvidia-* ─────────────────────────
    # Claude Desktop only renders models whose id starts with "claude-".
    # These aliases make NVIDIA models selectable in the model picker.
    # Nemotron family
    "claude-nvidia-nemotron-super-49b": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "claude-nvidia-nemotron-ultra-253b": "nvidia/llama-3.1-nemotron-ultra-253b-v1",
    "claude-nvidia-nemotron-70b": "nvidia/llama-3.1-nemotron-70b-instruct",
    "claude-nvidia-nemotron-nano-8b": "nvidia/llama-3.1-nemotron-nano-8b-v1",
    # Meta Llama
    "claude-nvidia-llama-70b": "meta/llama-3.3-70b-instruct",
    "claude-nvidia-llama-8b": "meta/llama-3.1-8b-instruct",
    "claude-nvidia-llama-maverick": "meta/llama-4-maverick-17b-128e-instruct",
    # DeepSeek
    "claude-nvidia-deepseek-v4-pro": "deepseek-ai/deepseek-v4-pro",
    "claude-nvidia-deepseek-v4-flash": "deepseek-ai/deepseek-v4-flash",
    # Mistral
    "claude-nvidia-mistral-large": "mistralai/mistral-large-2-instruct",
    "claude-nvidia-mistral-medium": "mistralai/mistral-medium-3.5-128b",
    # Qwen
    "claude-nvidia-qwen3-coder": "qwen/qwen3-coder-480b-a35b-instruct",
    "claude-nvidia-qwen3-80b": "qwen/qwen3-next-80b-a3b-instruct",
    # Moonshot / MoonshotAI
    "claude-nvidia-kimi-k2": "moonshotai/kimi-k2.6",
    # Google Gemma
    "claude-nvidia-gemma-4-31b": "google/gemma-4-31b-it",
    "claude-nvidia-gemma-3-12b": "google/gemma-3-12b-it",
}


def _load_yaml_config() -> dict[str, Any]:
    """Load non-secret config from YAML. Env vars remain authoritative.

    Set PROXY_CONFIG=/path/to/config.yaml to choose a file. If unset, a local
    config.yaml is used when present; otherwise defaults are used.
    """
    cfg_path = os.environ.get("PROXY_CONFIG")
    path = Path(cfg_path) if cfg_path else Path("config.yaml")
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"config file must contain a YAML mapping: {path}")
    return data


_CONFIG = _load_yaml_config()
_SERVER = _CONFIG.get("server") or {}
_NVIDIA = _CONFIG.get("nvidia") or {}
_STREAMING = _CONFIG.get("streaming") or {}
_CONTEXT = _CONFIG.get("context") or {}

NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY") or _NVIDIA.get("api_key")
NVIDIA_BASE_URL = os.environ.get("NVIDIA_BASE_URL") or _NVIDIA.get(
    "base_url", "https://integrate.api.nvidia.com/v1"
)
PROXY_HOST = os.environ.get("PROXY_HOST") or _SERVER.get("host", "127.0.0.1")
PROXY_PORT = int(os.environ.get("PROXY_PORT") or _SERVER.get("port", 8787))
PROXY_API_KEY = os.environ.get("PROXY_API_KEY") or _SERVER.get("api_key") or None
LOG_LEVEL = os.environ.get("LOG_LEVEL") or _SERVER.get("log_level", "info")
DEFAULT_MODEL = os.environ.get("DEFAULT_NVIDIA_MODEL") or _NVIDIA.get(
    "default_model", DEFAULT_NVIDIA_MODEL
)

MODEL_ALIASES = {**DEFAULT_MODEL_ALIASES}
MODEL_ALIASES.update(_CONFIG.get("model_aliases") or {})
for k, v in os.environ.items():
    if k.startswith("MODEL_ALIAS_") and v:
        alias = (
            k.removeprefix("MODEL_ALIAS_").lower().replace("__", "/").replace("_", "-")
        )
        MODEL_ALIASES[alias] = v
for alias in list(MODEL_ALIASES):
    if MODEL_ALIASES[alias] == DEFAULT_NVIDIA_MODEL:
        MODEL_ALIASES[alias] = DEFAULT_MODEL

# 15 s matches Anthropic's official cadence; keeps Claude Code's TUI alive
# during long reasoning phases (Nemotron Ultra can think for 30+ seconds).
PING_INTERVAL = float(_STREAMING.get("ping_interval", 15.0))

# Anthropic streams at ~sub-word granularity. NVIDIA emits 10–40-char chunks.
# We resplit on word/punctuation boundaries to recreate the "typing" feel.
TEXT_DELTA_CHARS = int(_STREAMING.get("text_delta_chars", 6))

# NVIDIA hosted endpoints reject input+output over the model context window.
# Claude Code commonly asks for ~16k output tokens, so keep a safety margin for
# tokenizer-estimation drift and upstream system/tool overhead.
MAX_OUTPUT_TOKENS = int(
    os.environ.get("MAX_OUTPUT_TOKENS") or _CONTEXT.get("max_output_tokens", 16384)
)
CONTEXT_SAFETY_MARGIN = int(
    os.environ.get("CONTEXT_SAFETY_MARGIN")
    or _CONTEXT.get("safety_margin_tokens", 2048)
)

# Static ISO timestamp for /v1/models created_at — per-call datetime.now()
# makes every response look like a different model version.
from datetime import datetime
from datetime import timezone as _tz

_PROXY_START_ISO = datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_model(requested: str) -> str:
    """Resolve a Claude Code requested model to an upstream NVIDIA model."""
    if requested in MODEL_ALIASES:
        return MODEL_ALIASES[requested]
    # Family-prefix fallback catches dated Claude Code aliases while preserving
    # native NVIDIA model IDs that users pass through directly.
    if requested.startswith(("claude-", "anthropic/claude-")):
        return DEFAULT_MODEL
    return requested


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


app = FastAPI(title="nvd-claude-proxy", version="1.0.0", lifespan=lifespan)


@app.middleware("http")
async def security_headers(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Cache-Control", "no-store")
    response.headers.setdefault("Permissions-Policy", "interest-cohort=()")
    return response


def JSONResp(data: dict[str, Any], status_code: int = 200) -> Response:
    return Response(
        content=orjson.dumps(data),
        status_code=status_code,
        media_type="application/json",
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

# STRICT protocol injection to prevent models from hallucinating tags
# instead of using the native tool-calling API.
_TOOL_PROTOCOL_SYSTEM_PROMPT = """
# Tool Use Protocol (STRICT)
You are an expert at tool use. You MUST ALWAYS follow these rules:
1. ALWAYS use the native `tool_calls` API for all tool interactions.
2. NEVER output tags like `<command-name>`, `<command-arguments>`, or similar.
3. If you need to call a tool, generate the `tool_calls` field in your response.
4. DO NOT explain your tool call or output any text before the tool call if possible.
5. If you hallucinate a tag, you will be stopped. Use ONLY JSON for tool calls.
"""


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
    return {"type": "text", "text": f"[unsupported image source type: {t}]"}


def translate_messages(messages: list[dict], tool_id_map: dict[str, str]) -> list[dict]:
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
                tool_calls.append(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input") or {}),
                        },
                    }
                )
            elif btype == "tool_result":
                tid = block["tool_use_id"]
                openai_id = tool_id_map.get(tid, tid)
                raw = block.get("content", "")
                if isinstance(raw, list):
                    raw = "".join(
                        b.get("text", "") for b in raw if b.get("type") == "text"
                    )
                tool_results.append(
                    {
                        "role": "tool",
                        "tool_call_id": openai_id,
                        "content": str(raw) if raw is not None else "",
                    }
                )
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
                out.append(
                    {
                        "role": "user",
                        "content": (
                            "".join(p["text"] for p in text_parts)
                            if all(p["type"] == "text" for p in text_parts)
                            else text_parts
                        ),
                    }
                )
            out.extend(tool_results)
    return out


_OPENAI_MAX_TOOL_NAME = 64  # OpenAI/NVIDIA hard limit; Anthropic allows 128


def _safe_tool_name(name: str, tool_name_map: dict[str, str]) -> str:
    """Truncate tool name to OpenAI's 64-char limit and record the mapping.

    LiteLLM documents this as a critical correctness issue: NVIDIA silently
    truncates longer names, so the response comes back with a different name
    than the request, breaking Claude Code's tool call round-trip.
    """
    if len(name) <= _OPENAI_MAX_TOOL_NAME:
        tool_name_map[name] = name
        return name
    short = name[:_OPENAI_MAX_TOOL_NAME]
    tool_name_map[short] = name  # preserve original for response translation
    return short


def translate_tools(
    tools: list[dict] | None,
    tool_name_map: dict[str, str] | None = None,
) -> list[dict] | None:
    if not tools:
        return None
    if tool_name_map is None:
        tool_name_map = {}
    out = []
    tool_count = len(tools)

    # Aggressively cap descriptions if many tools are present to fit context
    desc_cap = 480
    if tool_count > 100:
        desc_cap = 160
    elif tool_count > 40:
        desc_cap = 280

    for t in tools:
        # Skip Anthropic server-side tools — no NVIDIA equivalent
        if SERVER_TOOL_RE.search(t.get("type") or ""):
            continue

        desc = t.get("description") or ""
        if len(desc) > desc_cap:
            desc = desc[:desc_cap] + "..."

        out.append(
            {
                "type": "function",
                "function": {
                    "name": _safe_tool_name(t["name"], tool_name_map),
                    "description": desc,
                    "parameters": t.get("input_schema")
                    or {"type": "object", "properties": {}},
                },
            }
        )
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


def translate_request(
    body: dict,
    tool_id_map: dict[str, str],
    tool_name_map: dict[str, str] | None = None,
) -> dict:
    if tool_name_map is None:
        tool_name_map = {}
    msgs: list[dict] = []
    sys = flatten_system(body.get("system"))

    # Inject tool protocol if tools are present
    if body.get("tools"):
        if sys:
            sys = _TOOL_PROTOCOL_SYSTEM_PROMPT.strip() + "\n\n" + sys
        else:
            sys = _TOOL_PROTOCOL_SYSTEM_PROMPT.strip()

    if sys:
        msgs.append({"role": "system", "content": sys})

    msgs.extend(translate_messages(body.get("messages") or [], tool_id_map))

    # Clamp max_tokens so input + output never exceed this model's context window.
    # Uses per-model registry (LiteLLM pattern) so models with 202k+ windows are
    # handled correctly instead of all being capped at a single global constant.
    nvidia_model = resolve_model(body["model"])
    ctx_limit = _context_limit_for(nvidia_model)

    max_tokens = min(body.get("max_tokens") or 4096, MAX_OUTPUT_TOKENS)

    # Walk the full message tree to estimate input tokens — nested content blocks
    # (tool_result, image, thinking) would be missed by a flat .get("content") call.
    def _count_chars(obj) -> int:
        if isinstance(obj, str):
            return len(obj)
        if isinstance(obj, list):
            return sum(_count_chars(v) for v in obj)
        if isinstance(obj, dict):
            return sum(
                _count_chars(v)
                for k, v in obj.items()
                if k not in ("type", "media_type", "cache_control")
            )
        return 0

    estimated_input = _count_chars(msgs) // 4
    headroom = ctx_limit - estimated_input - CONTEXT_SAFETY_MARGIN
    if headroom < max_tokens:
        max_tokens = max(1, headroom)

    payload: dict = {
        "model": resolve_model(body["model"]),
        "messages": msgs,
        "max_tokens": max_tokens,
        "stream": bool(body.get("stream", False)),
    }
    for key in ("temperature", "top_p"):
        if (v := body.get(key)) is not None:
            payload[key] = v
    if (ss := body.get("stop_sequences")) is not None:
        payload["stop"] = ss
    if tools := translate_tools(body.get("tools"), tool_name_map):
        payload["tools"] = tools
        if (tc := translate_tool_choice(body.get("tool_choice"))) is not None:
            payload["tool_choice"] = tc
    # Forward Claude Code's metadata.user_id as OpenAI's `user`
    if (md := body.get("metadata")) and isinstance(md, dict):
        if uid := md.get("user_id"):
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


def extract_thinking(
    content: str | None, reasoning: str | None
) -> tuple[str | None, str]:
    """Return (thinking_text, remaining_content). Handles both surfaces:
    a separate `reasoning_content` field, or inline <think>…</think> tags."""
    if reasoning:
        return reasoning, content or ""
    if content and "<think>" in content:
        if m := THINK_RE.search(content):
            return m.group(1).strip(), THINK_RE.sub("", content, count=1).lstrip()
    return None, content or ""


def translate_response(
    oai: dict,
    model: str,
    tool_id_map: dict[str, str],
    tool_name_map: dict[str, str] | None = None,
) -> dict:
    choice = (oai.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    blocks: list[dict] = []

    thinking, remaining = extract_thinking(
        msg.get("content"), msg.get("reasoning_content")
    )
    if thinking:
        blocks.append(
            {
                "type": "thinking",
                "thinking": thinking,
                "signature": new_signature(),
            }
        )
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
        truncated_name = fn.get("name", "")
        original_name = (tool_name_map or {}).get(truncated_name, truncated_name)
        blocks.append(
            {
                "type": "tool_use",
                "id": anth_id,
                "name": original_name,
                "input": args,
            }
        )

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

    def __init__(
        self,
        model: str,
        tool_id_map: dict[str, str],
        tool_name_map: dict[str, str] | None = None,
    ):
        self.model = model
        self.tool_id_map = tool_id_map
        self.tool_name_map = tool_name_map or {}
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
            yield self._ev(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.open_index,
                    "delta": {
                        "type": "signature_delta",
                        "signature": new_signature(),
                    },
                },
            )
            self.signature_sent = True
        yield self._ev(
            "content_block_stop",
            {
                "type": "content_block_stop",
                "index": self.open_index,
            },
        )
        self.open_type = None
        self.open_index = None
        self.signature_sent = False

    def _open_text(self):
        idx = self.next_index
        self.next_index += 1
        self.open_type = "text"
        self.open_index = idx
        yield self._ev(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            },
        )

    def _open_thinking(self):
        idx = self.next_index
        self.next_index += 1
        self.open_type = "thinking"
        self.open_index = idx
        self.signature_sent = False
        yield self._ev(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "thinking", "thinking": "", "signature": ""},
            },
        )

    def _emit_text(self, s: str):
        if not s:
            return
        if self.open_type != "text":
            yield from self._close_open()
            yield from self._open_text()
        for piece in retokenize(s):
            yield self._ev(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.open_index,
                    "delta": {"type": "text_delta", "text": piece},
                },
            )

    def _emit_thinking(self, s: str):
        if not s:
            return
        if self.open_type != "thinking":
            yield from self._close_open()
            yield from self._open_thinking()
        for piece in retokenize(s):
            yield self._ev(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self.open_index,
                    "delta": {"type": "thinking_delta", "thinking": piece},
                },
            )

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
                buf = buf[idx + len(_THINK_CLOSE) :]
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
                buf = buf[idx + len(_THINK_OPEN) :]

    def feed(self, chunk: dict):
        # Trailing usage-only chunk (choices == [])
        if not chunk.get("choices"):
            if u := chunk.get("usage"):
                self.usage_in = u.get("prompt_tokens", self.usage_in)
                self.usage_out = u.get("completion_tokens", self.usage_out)
            return

        choice = chunk["choices"][0]
        delta = choice.get("delta") or {}
        finish = choice.get("finish_reason")

        if rc := delta.get("reasoning_content"):
            yield from self._emit_thinking(rc)
        if text := delta.get("content"):
            yield from self._process_text(text)

        for tc in delta.get("tool_calls") or []:
            o_idx = tc.get("index", 0)
            buf = self.tools.setdefault(
                o_idx,
                {
                    "oid": None,
                    "name": None,
                    "started": False,
                    "anth_id": None,
                    "anth_idx": None,
                },
            )
            fn = tc.get("function") or {}
            if i := tc.get("id"):
                buf["oid"] = i
            if n := fn.get("name"):
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
                original_name = self.tool_name_map.get(buf["name"], buf["name"])
                yield self._ev(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": buf["anth_idx"],
                        "content_block": {
                            "type": "tool_use",
                            "id": anth_id,
                            "name": original_name,
                            "input": {},
                        },
                    },
                )
            if buf["started"] and args:
                yield self._ev(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": buf["anth_idx"],
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": args,
                        },
                    },
                )

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
        yield self._ev(
            "message_delta",
            {
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
            },
        )
        yield self._ev("message_stop", {"type": "message_stop"})


# ─── HTTP routes ─────────────────────────────────────────────────────────────


def encode_sse(event: str, data: dict) -> bytes:
    return b"event: " + event.encode() + b"\ndata: " + orjson.dumps(data) + b"\n\n"


STATUS_TO_ERR = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
    504: "api_error",
    529: "overloaded_error",
}


def map_error_type(status: int) -> str:
    return STATUS_TO_ERR.get(status, "api_error")


def _nvidia_error_message(body: dict[str, Any] | str | bytes | bytearray) -> str:
    """Extract a human-readable message from NVIDIA/OpenAI error payloads.

    NVIDIA wraps errors as {"error": {"message": "...", "type": "BadRequestError"}}.
    Falls back to top-level "message"/"detail" and then raw text.
    """
    if isinstance(body, (bytes, bytearray)):
        try:
            body = json.loads(body)
        except Exception:
            return body.decode(errors="replace")[:500]
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except Exception:
            return body[:500]
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("detail") or err)[:500]
        return str(body.get("message") or body.get("detail") or body)[:500]
    return str(body)[:500]


# Matches NVIDIA's actual error format:
# "maximum context length is 131072 tokens. However, you requested 16384 output
#  tokens and your prompt contains at least 114689 input tokens"
_CTX_OVERFLOW_RE = re.compile(
    r"maximum\s+context\s+length\s+is\s+(\d+)\s+tokens.*?"
    r"requested\s+(\d+)\s+output\s+tokens.*?"
    r"(?:prompt\s+)?contains\s+at\s+least\s+(\d+)\s+input\s+tokens",
    re.IGNORECASE | re.DOTALL,
)

# Fallback for other wording variations from older NIM versions.
_CTX_OVERFLOW_RE2 = re.compile(
    r"passed\s+(\d+)\s+input\s+tokens\s+and\s+requested\s+(\d+).*?"
    r"context\s+length\s+is\s+(?:only\s+)?(\d+)\s+tokens",
    re.IGNORECASE | re.DOTALL,
)

# Per-model context window registry (same pattern as LiteLLM).
# Values are total context length in tokens (input + output).
# Keyed on the NVIDIA model ID as returned by /v1/models.
# Unknown models fall back to FALLBACK_CONTEXT_LIMIT.
FALLBACK_CONTEXT_LIMIT = 131072
MODEL_CONTEXT_REGISTRY: dict[str, int] = {
    # Nemotron
    "nvidia/llama-3.3-nemotron-super-49b-v1.5": 131072,
    "nvidia/llama-3.1-nemotron-ultra-253b-v1": 131072,
    "nvidia/llama-3.1-nemotron-70b-instruct": 131072,
    "nvidia/llama-3.1-nemotron-nano-8b-v1": 131072,
    # Meta Llama 3.x
    "meta/llama-3.3-70b-instruct": 131072,
    "meta/llama-3.1-70b-instruct": 131072,
    "meta/llama-3.1-8b-instruct": 131072,
    "meta/llama-3.2-90b-vision-instruct": 131072,
    "meta/llama-3.2-11b-vision-instruct": 131072,
    "meta/llama-3.2-3b-instruct": 131072,
    "meta/llama-3.2-1b-instruct": 131072,
    "meta/llama-4-maverick-17b-128e-instruct": 524288,
    # DeepSeek
    "deepseek-ai/deepseek-v4-pro": 163840,
    "deepseek-ai/deepseek-v4-flash": 163840,
    # Mistral
    "mistralai/mistral-large-2-instruct": 131072,
    "mistralai/mistral-medium-3.5-128b": 131072,
    "mistralai/mistral-large-3-675b-instruct-2512": 131072,
    "mistralai/mixtral-8x22b-instruct-v0.1": 65536,
    "mistralai/mixtral-8x7b-instruct-v0.1": 32768,
    # Qwen
    "qwen/qwen3-coder-480b-a35b-instruct": 131072,
    "qwen/qwen3-next-80b-a3b-instruct": 131072,
    "qwen/qwen3.5-122b-a10b": 131072,
    # Moonshot
    "moonshotai/kimi-k2.6": 131072,
    # Google Gemma
    "google/gemma-4-31b-it": 131072,
    "google/gemma-3-12b-it": 131072,
}


def _context_limit_for(nvidia_model_id: str) -> int:
    """Return the total context window size for a given NVIDIA model ID."""
    return MODEL_CONTEXT_REGISTRY.get(nvidia_model_id, FALLBACK_CONTEXT_LIMIT)


def _context_safe_max_tokens(err_msg: str) -> int | None:
    """Return a reduced max_tokens that fits inside the model context, or None."""
    m = _CTX_OVERFLOW_RE.search(err_msg)
    if m:
        ctx, input_toks = int(m.group(1)), int(m.group(3))
        safe = ctx - input_toks - max(CONTEXT_SAFETY_MARGIN, 1)
        return min(safe, MAX_OUTPUT_TOKENS) if safe > 0 else None
    m2 = _CTX_OVERFLOW_RE2.search(err_msg)
    if m2:
        input_toks, ctx = int(m2.group(1)), int(m2.group(3))
        safe = ctx - input_toks - max(CONTEXT_SAFETY_MARGIN, 1)
        return min(safe, MAX_OUTPUT_TOKENS) if safe > 0 else None
    return None


def check_auth(request: Request):
    if not PROXY_API_KEY:
        return
    presented = (
        request.headers.get("x-api-key")
        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )
    if not hmac.compare_digest(presented, PROXY_API_KEY):
        return JSONResp(
            {
                "type": "error",
                "error": {
                    "type": "authentication_error",
                    "message": "invalid proxy api key",
                },
            },
            status_code=401,
        )


@app.get("/healthz")
@app.get("/health")
async def healthz():
    return {"status": "ok"}


@app.get("/v1/models")
async def list_models(request: Request):
    """Return Anthropic-format model list.

    Claude Code and Claude Desktop validate the schema strictly:
    - each entry needs "type": "model" (not OpenAI's "object": "model")
    - top-level needs has_more / first_id / last_id pagination fields
    - created_at must be an ISO-8601 string, not a Unix timestamp

    We surface the proxy's Claude aliases first (so the model picker shows
    familiar claude-* names), followed by every real NVIDIA model from the
    upstream catalog.
    """
    auth = check_auth(request)
    if auth is not None:
        return auth

    nvidia: httpx.AsyncClient = request.app.state.nvidia
    try:
        r = await nvidia.get("/models")
        try:
            nvidia_data = r.json().get("data", [])
        except Exception:
            nvidia_data = []
    except httpx.HTTPError:
        nvidia_data = []

    def _to_anthropic(model_id: str, display: str | None = None) -> dict:
        return {
            "type": "model",
            "id": model_id,
            "display_name": display or model_id,
            "created_at": _PROXY_START_ISO,
        }

    # Claude alias entries come first — these are what Claude Code's model
    # picker sends, and the proxy knows how to resolve them.
    seen: set[str] = set()
    models = []
    for alias in MODEL_ALIASES:
        if alias not in seen:
            models.append(_to_anthropic(alias))
            seen.add(alias)

    # Append real NVIDIA catalog models in Anthropic format.
    for m in nvidia_data:
        mid = m.get("id", "")
        if mid and mid not in seen:
            models.append(_to_anthropic(mid))
            seen.add(mid)

    return JSONResp(
        {
            "data": models,
            "has_more": False,
            "first_id": models[0]["id"] if models else None,
            "last_id": models[-1]["id"] if models else None,
        }
    )


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Heuristic token count — NVIDIA's hosted catalog has no native endpoint
    for this. ~4 chars/token holds within ±15% for Llama/Nemotron/Qwen."""
    auth = check_auth(request)
    if auth is not None:
        return auth
    body = await request.json()

    def walk(o):
        if isinstance(o, dict):
            return sum(walk(v) for k, v in o.items() if k != "data")
        if isinstance(o, list):
            return sum(walk(v) for v in o)
        if isinstance(o, str):
            return len(o)
        return 0

    return JSONResp({"input_tokens": max(walk(body) // 4, 1)})


@app.post("/v1/messages")
async def messages(request: Request):
    auth = check_auth(request)
    if auth is not None:
        return auth
    try:
        body = await request.json()
    except Exception as e:
        return JSONResp(
            {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": f"bad JSON: {e}"},
            },
            status_code=400,
        )
    if "model" not in body or "messages" not in body:
        return JSONResp(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "model and messages are required",
                },
            },
            status_code=400,
        )

    tool_id_map: dict[str, str] = {}
    tool_name_map: dict[str, str] = {}
    payload = translate_request(body, tool_id_map, tool_name_map)
    nvidia: httpx.AsyncClient = request.app.state.nvidia

    # Non-streaming path
    if not body.get("stream"):
        try:
            resp = await nvidia.post("/chat/completions", json=payload)
        except httpx.HTTPError as e:
            return JSONResp(
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": str(e)[:500]},
                },
                status_code=502,
            )
        if resp.status_code >= 400:
            try:
                body_err = _nvidia_error_message(resp.json())
            except Exception:
                body_err = resp.text[:500]
            # Auto-retry once with reduced max_tokens on context-length overflow
            if resp.status_code == 400:
                safe = _context_safe_max_tokens(body_err)
                if safe is not None:
                    payload["max_tokens"] = safe
                    try:
                        resp = await nvidia.post("/chat/completions", json=payload)
                        if resp.status_code < 400:
                            return JSONResp(
                                translate_response(
                                    resp.json(),
                                    body["model"],
                                    tool_id_map,
                                    tool_name_map,
                                )
                            )
                        body_err = _nvidia_error_message(resp.json())
                    except httpx.HTTPError:
                        pass
            return JSONResp(
                {
                    "type": "error",
                    "error": {
                        "type": map_error_type(resp.status_code),
                        "message": body_err,
                    },
                },
                status_code=resp.status_code,
            )
        return JSONResp(
            translate_response(resp.json(), body["model"], tool_id_map, tool_name_map)
        )

    # Streaming path
    return StreamingResponse(
        stream_response(request, body, payload, tool_id_map, tool_name_map),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # defeat nginx/proxy buffering
            "Content-Encoding": "identity",  # never gzip an SSE stream
        },
    )


async def stream_response(
    request: Request,
    body: dict,
    payload: dict,
    tool_id_map: dict[str, str],
    tool_name_map: dict[str, str] | None = None,
) -> AsyncIterator[bytes]:
    """The hot path. Three things to remember:
    1. Emit message_start IMMEDIATELY — don't wait for NVIDIA's first chunk.
    2. Ping every 15 s while idle so Claude Code's TUI never freezes.
    3. Cancel the upstream task on client disconnect.
    """
    nvidia: httpx.AsyncClient = request.app.state.nvidia
    model = payload["model"]
    msg_id = new_msg_id()

    # 1) Eager message_start — synchronous yield before any await.
    yield encode_sse(
        "message_start",
        {
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
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
    )

    st = StreamTranslator(
        model=model, tool_id_map=tool_id_map, tool_name_map=tool_name_map or {}
    )
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
            for attempt in range(2):
                async with nvidia.stream(
                    "POST", "/chat/completions", json=payload
                ) as r:
                    if r.status_code >= 400:
                        body_bytes = await r.aread()
                        try:
                            err_body = json.loads(body_bytes)
                        except Exception:
                            err_body = body_bytes.decode(errors="replace")
                        # Auto-retry once on context-length overflow
                        if r.status_code == 400 and attempt == 0:
                            safe = _context_safe_max_tokens(
                                _nvidia_error_message(err_body)
                            )
                            if safe is not None:
                                payload["max_tokens"] = safe
                                continue
                        await queue.put((ERROR, r.status_code, err_body))
                        return
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
                    return  # success — stop retry loop
        except Exception as e:  # noqa: BLE001
            await queue.put((ERROR, None, str(e)))
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
                _, status, err_body = item
                msg = (
                    _nvidia_error_message(err_body)
                    if isinstance(err_body, (dict, str, bytes))
                    else str(err_body)[:500]
                )
                yield encode_sse(
                    "error",
                    {
                        "type": "error",
                        "error": {
                            "type": map_error_type(status or 500),
                            "message": msg,
                        },
                    },
                )
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
    loop = "auto"
    http = "auto"
    try:
        import uvloop  # noqa: F401

        loop = "uvloop"
    except ImportError:
        pass
    try:
        import httptools  # noqa: F401

        http = "httptools"
    except ImportError:
        pass

    print(f"\nnvd-claude-proxy v1.0")
    print(f"  listen      → http://{PROXY_HOST}:{PROXY_PORT}")
    print(f"  upstream    → {NVIDIA_BASE_URL}")
    print(f"  default     → {DEFAULT_MODEL}")
    print(f"  aliases     → {len(MODEL_ALIASES)} configured")
    print(
        f"  point Claude Code's ANTHROPIC_BASE_URL at http://{PROXY_HOST}:{PROXY_PORT}\n"
    )
    uvicorn.run(
        app,
        host=str(PROXY_HOST),
        port=int(PROXY_PORT),
        log_level=str(LOG_LEVEL),
        access_log=False,
        loop=loop,
        http=http,
    )


if __name__ == "__main__":
    main()
