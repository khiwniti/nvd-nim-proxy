# Project Structure

**Analysis Date:** 2026-05-13

## Directory Layout

```
nim-proxy/
├── proxy.py                    # Core proxy server — Anthropic → NVIDIA NIM translation
├── nim_code.py                 # CLI entry point (nim code / nim proxy / nim status / …)
├── pyproject.toml              # Package metadata; defines `nim` and `nim-proxy` scripts
├── requirements.txt            # pip install list (mirrors pyproject.toml dependencies)
├── config.example.yaml         # Annotated config template; copy to config.yaml
├── .env.example                # Annotated env var template; copy to .env
├── CLAUDE.md                   # Project instructions for Claude Code
├── README.md                   # User-facing documentation
├── tests/
│   ├── conftest.py             # Sets NVIDIA_API_KEY=test-key-for-pytest before import
│   ├── test_translation.py     # Unit tests: resolve_model, translate_request/response, error mapping
│   ├── test_streaming.py       # Unit tests: StreamTranslator event ordering, thinking tags, tool deltas
│   ├── test_stream_eager.py    # Async test: message_start emitted before upstream first chunk (SC-002)
│   ├── test_routes.py          # Route smoke tests: /healthz, /v1/messages validation via TestClient
│   └── test_e2e.py             # End-to-end tests (requires live proxy)
├── specs/
│   └── 001-claude-nvidia-proxy/
│       ├── spec.md             # Feature specification
│       ├── plan.md             # Implementation plan (current plan per CLAUDE.md)
│       ├── tasks.md            # Task breakdown
│       ├── requirements.md     # Requirements
│       ├── data-model.md       # Data model definitions
│       ├── research.md         # Research notes
│       ├── quickstart.md       # Quickstart guide
│       ├── checklists/         # Spec acceptance checklists
│       └── contracts/          # API contract definitions
│           └── anthropic-messages.md
├── .planning/
│   └── codebase/               # GSD codebase map documents (this directory)
├── .claude/
│   └── skills/                 # GSD skill definitions (speckit-*)
├── .specify/                   # Specify workflow tooling
├── dist/                       # Built wheel/sdist artifacts (generated, not committed)
└── Building a custom proxy for Claude Code.md   # Source reference document
```

## Key Files

**Entry Points:**
- `proxy.py`: Run directly with `python proxy.py` or via `nim-proxy` console script. All proxy logic is in this one file.
- `nim_code.py`: Run via `nim` console script. Orchestrates starting proxy + Claude Code together.

**Configuration:**
- `config.example.yaml`: Canonical reference for all YAML config options. Copy to `config.yaml` to activate. Sections: `server`, `nvidia`, `streaming`, `model_aliases`.
- `.env.example`: Template for `NVIDIA_API_KEY` and `PROXY_PORT`. Loaded automatically by `nim_code.py`'s `load_env()` function.
- `pyproject.toml`: Defines package name `nvd-claude-nim` v0.1.5, dependencies, and two console scripts: `nim` → `nim_code:main` and `nim-proxy` → `proxy:main`.

**Core Logic:**
- `proxy.py` contains all proxy logic — config loading, model alias resolution, Anthropic→OpenAI translation, streaming state machine, FastAPI routes, httpx client lifecycle. No other source files are needed at runtime.

**Testing:**
- `tests/conftest.py`: Module-level env setup shared by all test files.
- `tests/test_translation.py`: Pure-function unit tests; no HTTP.
- `tests/test_streaming.py`: StreamTranslator unit tests; no HTTP.
- `tests/test_stream_eager.py`: Async test using stub NVIDIA client; verifies eager `message_start` before upstream response.
- `tests/test_routes.py`: FastAPI `TestClient`-based route tests; offline.
- `tests/test_e2e.py`: Requires a running proxy with a valid `NVIDIA_API_KEY`.

## Module Organization

There are only two Python source modules:

### `proxy.py` — sections in order

| Section | Lines | Contents |
|---------|-------|----------|
| Config | 53–116 | `_load_yaml_config()`, YAML/env merge, `MODEL_ALIASES`, `PING_INTERVAL`, `TEXT_DELTA_CHARS` |
| Model resolution | 118–126 | `resolve_model()` |
| Lifespan | 130–155 | `@asynccontextmanager lifespan` — creates/closes shared httpx `AsyncClient` |
| FastAPI app | 158–163 | `app = FastAPI(...)` |
| ID helpers | 167–179 | `new_msg_id()`, `new_tool_id()`, `new_signature()` |
| Request translation | 182–399 | `flatten_system()`, `image_block_to_openai()`, `translate_messages()`, `translate_tools()`, `translate_tool_choice()`, `translate_request()` |
| Response translation | 402–474 | `THINK_RE`, `extract_thinking()`, `translate_response()` |
| Streaming state machine | 477–735 | `retokenize()`, `_safe_suffix_len()`, `StreamTranslator` class |
| HTTP routes | 738–966 | `encode_sse()`, `check_auth()`, route handlers (`/healthz`, `/v1/models`, `/v1/messages/count_tokens`, `/v1/messages`), `stream_response()` async generator |
| Entrypoint | 969–999 | `main()` — uvicorn startup with optional uvloop/httptools |

### `nim_code.py` — subcommands

| Subcommand | Function | Action |
|------------|----------|--------|
| `nim code` | `cmd` in `main()` | Start proxy in background, wait for health, launch `claude` |
| `nim proxy` | imports `proxy.main` | Start proxy in foreground |
| `nim status` | `cmd_status()` | Check `/healthz` and model count |
| `nim models` | `cmd_models()` | Fetch and print `/v1/models` |
| `nim test` | `cmd_test()` | POST a test message and print response |
| `nim init` | `cmd_init()` | Interactive wizard: prompt for API key, write `.env` |
| `nim version` | inline | Print `nvd-claude-nim CLI v0.1.4` |

## Entry Points

**Running the proxy server directly:**
```bash
NVIDIA_API_KEY=nvapi-... python proxy.py
# or after pip install:
NVIDIA_API_KEY=nvapi-... nim-proxy
```

**Running proxy + Claude Code together:**
```bash
nim code
# or with model override:
nim code --model meta/llama-3.3-70b-instruct
```

**Running tests:**
```bash
pytest tests/                    # all offline tests
pytest tests/test_e2e.py         # requires live NVIDIA_API_KEY
pytest --cov=proxy tests/        # with coverage
```

## Where to Add New Code

**New HTTP route:** Add a `@app.get` or `@app.post` decorated function in `proxy.py` in the "HTTP routes" section (after line 738). Follow the `check_auth(request)` + `ORJSONResponse(...)` pattern.

**New model alias:** Add to `DEFAULT_MODEL_ALIASES` dict (`proxy.py`, line 56) or to `model_aliases` section of `config.example.yaml`. Do not hard-code NVIDIA model IDs in multiple places.

**New translation logic (request side):** Extend `translate_request()` (`proxy.py`, line 359) or the relevant sub-function (`translate_messages`, `translate_tools`, `translate_tool_choice`).

**New translation logic (response side):** Extend `translate_response()` (`proxy.py`, line 426) for non-streaming, and `StreamTranslator.feed()` / `StreamTranslator.finalize()` for streaming.

**New CLI subcommand:** Add a parser and `cmd_*` function in `nim_code.py` and wire it into `main()`.

**New unit tests:** Add to the appropriate file in `tests/`. Pure function tests go in `test_translation.py` or `test_streaming.py`. Route tests go in `test_routes.py`. Async streaming tests go in `test_stream_eager.py`.

**New config option:** Add to `config.example.yaml` with a comment, read it in `proxy.py` at module load time using `_CONFIG.get(...)`, and respect env var override precedence.

---

*Structure analysis: 2026-05-13*
