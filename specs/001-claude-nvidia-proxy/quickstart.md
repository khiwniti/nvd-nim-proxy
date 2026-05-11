# Quickstart: Claude NVIDIA Proxy

## 1. Configure

```bash
cp .env.example .env
$EDITOR .env
cp config.example.yaml config.yaml
```

Set `NVIDIA_API_KEY` in `.env` or your shell. Keep secrets out of git.

## 2. Install

```bash
python3 -m pip install -r requirements.txt
```

## 3. Run tests

```bash
python3 -m pytest -q
```

## 4. Start proxy

```bash
NVIDIA_API_KEY=$(grep ^NVIDIA_API_KEY .env | cut -d= -f2) python3 proxy.py
```

By default it listens on `http://127.0.0.1:8787`.

## 5. Test without Claude Code

```bash
curl -s http://127.0.0.1:8787/healthz

curl -s http://127.0.0.1:8787/v1/messages \
  -H "content-type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 64,
    "messages": [{"role": "user", "content": "Say hi in five words."}]
  }' | python3 -m json.tool
```

## 6. Point Claude Code at the proxy

```bash
M=nvidia/llama-3.3-nemotron-super-49b-v1.5
export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
export ANTHROPIC_API_KEY=not-used
export ANTHROPIC_CUSTOM_MODEL_OPTION=$M
export ANTHROPIC_CUSTOM_MODEL_OPTION_NAME="NVIDIA Nemotron Super 49B"
export ANTHROPIC_CUSTOM_MODEL_OPTION_DESCRIPTION="NVIDIA NIM hosted model"
export ANTHROPIC_DEFAULT_HAIKU_MODEL=$M
export ANTHROPIC_DEFAULT_OPUS_MODEL=$M
export ANTHROPIC_DEFAULT_SONNET_MODEL=$M
export CLAUDE_CODE_SUBAGENT_MODEL=$M
export CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
claude
```

## 7. Validate streaming

```bash
curl -sN http://127.0.0.1:8787/v1/messages \
  -H "content-type: application/json" \
  -H "anthropic-version: 2023-06-01" \
  -d '{
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 128,
    "stream": true,
    "messages": [{"role": "user", "content": "Count to ten slowly."}]
  }'
```

