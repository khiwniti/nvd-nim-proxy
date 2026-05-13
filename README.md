<div align="center">

# nvd-nim-proxy

**Run Claude Code on NVIDIA's free hosted AI catalog — no Anthropic subscription needed.**

[![Python](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)](https://python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![PyPI](https://img.shields.io/badge/pypi-nvd--claude--nim-orange?logo=pypi&logoColor=white)](https://pypi.org/project/nvd-claude-nim)
[![Tests](https://img.shields.io/badge/tests-20%20passing-brightgreen)](tests/)

```
Claude Code ──/v1/messages──► nvd-nim-proxy ──/v1/chat/completions──► integrate.api.nvidia.com
  (Anthropic SSE protocol)     (translation)      (OpenAI SSE protocol)        (NVIDIA NIM)
```

*One command. Free API key. Full Claude Code experience backed by Nemotron.*

</div>

---

## Why this exists

`integrate.api.nvidia.com` speaks **OpenAI Chat Completions**. Claude Code speaks **Anthropic Messages**. This proxy sits between them and translates everything — streaming SSE events, tool calls, vision, reasoning blocks, error envelopes — so Claude Code never knows the difference.

> **Note:** If you can run a NIM container yourself (single H100 or L40S), you don't need this proxy — see NVIDIA's [official Claude Code integration guide](https://docs.nvidia.com/nim/large-language-models/latest/ai-assistant-integrations/claude-code.html). This is for the **free hosted catalog** at `build.nvidia.com`.

---

## Quickstart — 2 minutes

```bash
# 1. Install
pip install nvd-claude-nim

# 2. Configure (guided wizard)
nim init
#  🔑 Enter NVIDIA_API_KEY (get one free at https://build.nvidia.com)
#  🔌 Proxy port [8787]

# 3. Start the proxy daemon
nim start
#  ● Proxy started  PID 12345  http://127.0.0.1:8787
#
#  ┌─ Claude Code env vars ──────────────────────────────┐
#  │  export ANTHROPIC_BASE_URL=http://127.0.0.1:8787    │
#  │  export ANTHROPIC_API_KEY=not-used                  │
#  └─────────────────────────────────────────────────────┘

# 4. Launch Claude Code (proxy keeps running between sessions)
nim code
```

**Or skip the daemon** and just use the one-liner:

```bash
NVIDIA_API_KEY=nvapi-... nim code
```

---

## CLI Reference

| Command | Description |
|---|---|
| `nim init` | Interactive setup wizard — saves config to `~/.config/nim-proxy/` |
| `nim start` | Start proxy as background daemon |
| `nim stop` | Stop the daemon |
| `nim restart` | Restart daemon |
| `nim status` | Show PID, URL, model, API key, health |
| `nim logs [-f] [-n N]` | View proxy logs; `-f` tails live |
| `nim code [--model ID]` | Start daemon if needed, then launch Claude Code |
| `nim doctor` | Diagnose: Python, key, NVIDIA API, port, health, Claude install |
| `nim configure <key> <val>` | Set a config value (`server.port`, `nvidia.default_model`, …) |
| `nim configure --list` | Print effective config (secrets redacted) |
| `nim models` | List available NVIDIA NIM models |
| `nim test [prompt]` | Send a one-shot test request and show the result |
| `nim proxy` | Start proxy in foreground (debugging) |
| `nim version` | Print version |

---

## Recommended Models

| Model | Best for |
|---|---|
| `nvidia/llama-3.3-nemotron-super-49b-v1.5` | **Default.** Best reasoning + tools balance |
| `nvidia/llama-3.1-nemotron-ultra-253b-v1` | Strongest reasoning — slower TTFT |
| `nvidia/nvidia-nemotron-nano-9b-v2` | Fast responses; good for sub-agent (`HAIKU_MODEL`) |
| `meta/llama-3.3-70b-instruct` | General purpose, no reasoning overhead |
| `qwen/qwen3-235b-a22b` | Strong coder, MoE architecture |
| `meta/llama-4-maverick-17b-128e-instruct` | Vision + tools |

> ⚠️ Avoid `deepseek-ai/deepseek-r1` — its tool-calling and reasoning paths are mutually exclusive on the hosted endpoint.

---

## Configuration

Config is stored at `~/.config/nim-proxy/config.yaml` and can be edited directly or via `nim configure`:

```bash
nim configure server.port 9000
nim configure nvidia.default_model nvidia/llama-3.1-nemotron-ultra-253b-v1
nim configure --list   # print all settings (key redacted)
```

**Environment variables** override YAML and are never written to disk:

```bash
export NVIDIA_API_KEY=nvapi-...           # required
export DEFAULT_NVIDIA_MODEL=nvidia/...    # override default model
export PROXY_HOST=127.0.0.1
export PROXY_PORT=8787
export PROXY_API_KEY=secret              # optional: require x-api-key from clients
export LOG_LEVEL=info
```

**Model aliases** in `config.example.yaml` map Claude Code model names to NVIDIA models automatically — no need to set `ANTHROPIC_DEFAULT_*_MODEL` manually when using `nim code`.

---

## What's translated

| Feature | Status |
|---|---|
| Streaming `/v1/messages` | ✅ Full SSE event sequence |
| Non-streaming `/v1/messages` | ✅ |
| Tool calling (single + parallel) | ✅ `tool_use` ↔ `tool_calls` |
| `tool_result` round-trip | ✅ |
| System prompts (string + block array) | ✅ |
| Vision (base64 + URL) | ✅ |
| Reasoning (`reasoning_content` + `<think>` tags) | ✅ |
| Token counting (`/v1/messages/count_tokens`) | ✅ heuristic ±15% |
| Model listing (`/v1/models`) | ✅ proxied |
| Eager `message_start` (sub-100 ms TTFT) | ✅ |
| 15 s ping heartbeat during reasoning | ✅ keeps TUI alive |
| HTTP/2 to NVIDIA | ✅ when `h2` installed |
| Client-disconnect cancellation | ✅ |
| Prompt caching cost savings | ❌ not available on hosted endpoint |
| Anthropic server tools (`web_search_*`, `computer_*`, MCP) | ❌ no NVIDIA equivalent |

---

## Troubleshooting

Run `nim doctor` first — it checks everything in one go.

**"Long pause before first token"**
Fixed by eager `message_start`. If still slow, NVIDIA's TTFT for Nemotron Ultra 253B is 3–8 s by design. Switch to Nemotron Super 49B v1.5 for snappier responses.

**`404` on `claude-haiku-4-5` or similar**
Use `nim code` instead of setting env vars manually — it sets all four `ANTHROPIC_DEFAULT_*_MODEL` vars correctly.

**`429 rate_limit_error`**
Free tier is 40 RPM per key. Back off or upgrade to [NVIDIA AI Enterprise](https://www.nvidia.com/en-us/data-center/products/ai-enterprise/).

**`401 authentication_error` from upstream**
Your `NVIDIA_API_KEY` is wrong or expired. Generate a new one at [build.nvidia.com](https://build.nvidia.com).

**Port already in use**
```bash
nim configure server.port 8788
nim restart
```

---

## Manual / Development Setup

```bash
git clone https://github.com/khiwniti/nvd-nim-proxy
cd nvd-nim-proxy
pip install -r requirements.txt

cp .env.example .env
# edit .env — paste NVIDIA_API_KEY

python3 nim_code.py code   # or: python3 proxy.py
```

**Run tests:**

```bash
python3 -m pytest -v          # 20 tests, no live API needed
python3 -m pytest --cov=proxy --cov-report=term-missing
```

**Test with curl (no Claude Code needed):**

```bash
# Non-streaming
curl -s http://127.0.0.1:8787/v1/messages \
  -H "content-type: application/json" \
  -d '{"model":"nvidia/llama-3.3-nemotron-super-49b-v1.5","max_tokens":64,
       "messages":[{"role":"user","content":"Say hi in five words."}]}' \
  | python3 -m json.tool

# Streaming — message_start should arrive in < 100 ms
curl -sN http://127.0.0.1:8787/v1/messages \
  -H "content-type: application/json" \
  -d '{"model":"nvidia/llama-3.3-nemotron-super-49b-v1.5","max_tokens":128,
       "stream":true,"messages":[{"role":"user","content":"Count to ten."}]}'
```

---

## Repository Layout

```
proxy.py              Anthropic → NVIDIA translation proxy (FastAPI)
nim_code.py           Production CLI — daemon, doctor, configure, etc.
config.example.yaml   Non-secret config with model aliases
.env.example          Environment variable template
requirements.txt      Runtime + test dependencies
pyproject.toml        Package metadata and build config
tests/
  conftest.py         Test env setup
  test_translation.py Request/response/error translation unit tests
  test_streaming.py   SSE event ordering and StreamTranslator tests
  test_stream_eager.py Eager message_start async test
  test_routes.py      Route smoke tests
  test_e2e.py         End-to-end tests with mocked NVIDIA API
specs/                Spec Kit — requirements, design, tasks
```

---

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

Built for developers who want Claude Code's full power on NVIDIA's free hosted models.

**[Get your free NVIDIA API key →](https://build.nvidia.com)**

</div>
