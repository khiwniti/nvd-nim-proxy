# nvd-claude-proxy

A small Anthropic Messages → NVIDIA NIM proxy that lets [Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview)
use the hosted catalog at [build.nvidia.com](https://build.nvidia.com)
(`integrate.api.nvidia.com`).

**One file. ~600 lines. No model registry, no schema layer, no production
hardening ceremony.** Just enough translation to make Claude Code feel right.

```
Claude Code  ── /v1/messages ──►  proxy.py  ── /v1/chat/completions ──►  integrate.api.nvidia.com
   (CLI)       (Anthropic SSE)               (OpenAI SSE)                      (NVIDIA NIM)
```

## Why this exists

`integrate.api.nvidia.com` speaks OpenAI Chat Completions only; Claude Code
speaks Anthropic Messages. NIM has been adding a native `/v1/messages`
endpoint to the *self-hosted* container, but the **hosted catalog has not
yet exposed it** — so a translation layer is still required for the free
hosted path.

If you can run a NIM container yourself (single H100 or L40S), you don't
need this proxy at all — see NVIDIA's [official Claude Code integration
guide](https://docs.nvidia.com/nim/large-language-models/latest/ai-assistant-integrations/claude-code.html).

## Quickstart

### Option 1: Install via PyPI (Recommended)

```bash
# 1. Install nvd-claude-nim
pip install nvd-claude-nim

# 2. Set your NVIDIA API key (get it at https://build.nvidia.com)
export NVIDIA_API_KEY=nvapi-...

# 3. Start Claude Code with the proxy in one command
nim code
```

### Option 2: Manual Setup (Development)

```bash
# 1. Clone and install dependencies
git clone https://github.com/nvidia/nim-proxy
cd nim-proxy
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
$EDITOR .env   # paste NVIDIA_API_KEY

# 3. Run the CLI locally
python3 nim_code.py code
```

## CLI Reference

```
nim start                   Start proxy as background daemon
nim stop                    Stop the daemon
nim restart                 Restart daemon
nim status                  Show daemon state, PID, URL, model
nim logs [-f] [-n N]        Show proxy logs (tail with -f)
nim code [--model ID]       Start daemon (if needed) + launch Claude Code
nim init                    Interactive setup wizard (saves to ~/.config/nim-proxy/)
nim doctor                  Diagnose: API key, port, reachability, Claude Code install
nim configure <key> <val>   Set config value (e.g. server.port, nvidia.default_model)
nim configure --list        Print effective config (secrets redacted)
nim models                  List NVIDIA models available through the proxy
nim test [prompt]           Send a one-shot test message and print the result
nim proxy                   Start proxy in foreground (for debugging)
nim version                 Print version
```

### 2-minute quickstart

```bash
pip install nvd-claude-nim   # 1. install

nim init                      # 2. enter NVIDIA_API_KEY, choose port

nim start                     # 3. start daemon
#  ✅ Proxy started (PID 12345) at http://127.0.0.1:8787
#  export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
#  export ANTHROPIC_API_KEY=not-used

nim code                      # 4. launch Claude Code (proxy already running)
```

Run `nim doctor` to diagnose problems. Run `nim stop` when done.

## Configuration

| Model ID                                          | Why pick it                              |
|---------------------------------------------------|------------------------------------------|
| `nvidia/llama-3.3-nemotron-super-49b-v1.5`        | Best default. Strong reasoning + tools.  |
| `nvidia/llama-3.1-nemotron-ultra-253b-v1`         | Strongest reasoning. Slower TTFT.        |
| `nvidia/nvidia-nemotron-nano-9b-v2`               | Fast. Use as `HAIKU_MODEL` if splitting. |
| `meta/llama-3.3-70b-instruct`                     | General-purpose, no reasoning.           |
| `qwen/qwen3-235b-a22b`                            | Strong on code, MoE.                     |
| `meta/llama-4-maverick-17b-128e-instruct`         | Vision + tools.                          |

Avoid `deepseek-ai/deepseek-r1` for Claude Code — its tool-calling and
reasoning paths are mutually incompatible on the hosted endpoint.

## Configuration

The proxy reads `config.yaml` by default when present, or the file named by
`PROXY_CONFIG=/path/to/config.yaml`. Environment variables override YAML for
secrets and deployment-specific settings.

`config.example.yaml` includes safe defaults and model aliases that map common
Claude Code model IDs to NVIDIA model IDs. This prevents accidental upstream
404s when Claude Code falls back to names such as `claude-3-5-sonnet-20241022`.

Useful environment variables:

```bash
export NVIDIA_API_KEY=nvapi-...              # required unless set in config.yaml
export PROXY_CONFIG=config.yaml              # optional
export DEFAULT_NVIDIA_MODEL=nvidia/llama-3.3-nemotron-super-49b-v1.5
export PROXY_HOST=127.0.0.1
export PROXY_PORT=8787
```

For Claude Code, still set `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` if your
client sends beta headers that third-party gateways reject.

## What works

- Streaming + non-streaming `/v1/messages`
- Tool calling (single and parallel; `tool_use` ↔ `tool_calls`)
- `tool_result` round-trip
- System prompts (string + block array)
- Vision (base64 + URL)
- Reasoning models (both `reasoning_content` and inline `<think>` tags)
- `count_tokens` (heuristic, ±15% accurate)
- `/v1/models` passthrough
- HTTP/2 to NVIDIA when `h2` is installed
- Eager `message_start` (sub-100 ms TTFT)
- 15 s ping heartbeat during silent reasoning phases
- Soft re-tokenization for "official-feel" streaming
- Client-disconnect cancellation

## What doesn't work (and won't, on this endpoint)

- **Prompt caching cost savings** — NVIDIA's hosted catalog has no
  ephemeral-cache pricing. `cache_control` markers are stripped silently.
- **`thinking.signature` round-trip** — proxy-generated signatures don't
  validate against the real Anthropic API. Don't proxy through us into
  Anthropic.
- **Anthropic server tools** (`web_search_*`, `computer_*`, `bash_*`,
  `code_execution_*`, `memory_*`, MCP via `anthropic-beta`) — these are
  Anthropic-managed services with no NVIDIA equivalent. Claude Code's
  client-side tools (Read/Write/Bash/Edit/Glob/Grep) work fine.
- **Free-tier rate limit (40 RPM)** — agentic tool loops will sometimes
  hit 429. The proxy passes the error through; Claude Code retries.

## Troubleshooting

**"Streaming feels chunky / not like the real Claude"**
Confirmed fixed in this version. If you still see it, your terminal may be
buffering — try `claude --no-spinner` to compare. The proxy emits ≤6-char
text deltas with sub-word boundaries.

**"Long pause before any token, then a flood"**
Confirmed fixed: `message_start` fires immediately, ping fires every 15 s.
If you still see a 5+ s pause, NVIDIA's model TTFT itself is the
bottleneck (Nemotron Ultra 253B can take 3–8 s to start producing tokens).
Switch to Nemotron Super 49B v1.5 for snappier interaction.

**404 on `claude-haiku-4-5-20251001`**
You forgot one of the `ANTHROPIC_DEFAULT_*_MODEL` env vars. All four (haiku,
sonnet, opus, subagent) must point at the same NVIDIA model ID.

**`429 rate_limit_error`**
Free tier is 40 RPM globally per key. Either back off, or upgrade to NVIDIA
AI Enterprise.

**`401 authentication_error` from upstream**
Your `NVIDIA_API_KEY` is wrong or expired. Get a new one at
[build.nvidia.com](https://build.nvidia.com).

## Files

```
proxy.py              # the proxy and translation layer
config.example.yaml   # non-secret config with model aliases
requirements.txt      # runtime + test dependencies
.env.example          # environment variables
README.md             # this file
specs/001-claude-nvidia-proxy/
                      # Spec Kit plan, contracts, tasks, quickstart
tests/                # pytest translation/streaming tests
```

## Test it without Claude Code

```bash
# Non-streaming
curl -s http://127.0.0.1:8787/v1/messages \
  -H "content-type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Say hi in five words."}]
  }' | python3 -m json.tool

# Streaming (you should see message_start arrive in <100 ms)
curl -sN http://127.0.0.1:8787/v1/messages \
  -H "content-type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
    "max_tokens": 128,
    "stream": true,
    "messages": [{"role": "user", "content": "Count to ten slowly."}]
  }'
```

## License

MIT.

