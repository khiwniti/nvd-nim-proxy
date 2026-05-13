# Tech Stack

**Analysis Date:** 2026-05-13

## Language & Runtime

**Primary:**
- Python 3.9+ — entire application (`proxy.py`, `nim_code.py`)
  - Minimum version enforced in `pyproject.toml`: `requires-python = ">=3.9"`
  - Uses `from __future__ import annotations` for deferred type evaluation
  - Uses walrus operator (`:=`), `str.removeprefix` — requires 3.9+

**Optional performance runtime:**
- `uvloop` — faster asyncio event loop (imported at startup in `proxy.py` lines 979–981, used if installed)
- `httptools` — faster HTTP parsing for uvicorn (imported at startup in `proxy.py` lines 983–986, used if installed)
- `h2` — HTTP/2 support for upstream NVIDIA connections (imported in lifespan `proxy.py` lines 136–139, used if installed)

## Frameworks & Libraries

**Web Framework:**
- `fastapi>=0.115` — ASGI web framework; powers all HTTP routes
  - Routes: `GET /healthz`, `GET /v1/models`, `POST /v1/messages`, `POST /v1/messages/count_tokens`
  - Uses `lifespan` context manager for startup/shutdown
  - Default response class: `ORJSONResponse`

**ASGI Server:**
- `uvicorn[standard]>=0.32` — serves the FastAPI app
  - Optional `[standard]` extras enable `uvloop` and `httptools` if available
  - Configured via `proxy.main()` with `host`, `port`, `log_level`

**HTTP Client:**
- `httpx[http2]>=0.27` — async HTTP client for proxying requests to NVIDIA
  - One shared `httpx.AsyncClient` per process (created in lifespan)
  - Timeout: connect=10s, read=600s, write=30s
  - Connection pool: max 100, max keepalive 20, expiry 60s
  - HTTP/2 enabled when `h2` package is present

**JSON Serialization:**
- `orjson>=3.10` — fast JSON; used for all response serialization and SSE encoding
  - `ORJSONResponse` used as FastAPI default response class
  - `orjson.dumps()` used in `encode_sse()` (`proxy.py` line 741)

**YAML Config:**
- `PyYAML>=6.0` — loads optional `config.yaml` non-secret configuration (`proxy.py` lines 67–82)

**Standard Library (key modules):**
- `asyncio` — async producer/consumer queue for streaming (`proxy.py` lines 901–931)
- `re` — regex for thinking-tag parsing and server-tool filtering
- `secrets` — cryptographically secure ID generation for message/tool IDs
- `json` — upstream JSON parsing in streaming path (stdlib, not orjson, for chunk parsing)
- `pathlib.Path` — config file resolution

## Build & Package Management

**Build Backend:**
- `hatchling` — PEP 517 build backend declared in `pyproject.toml`

**Package Metadata:**
- `pyproject.toml` — canonical project definition
  - Package: `nvd-claude-nim` v0.1.5
  - License: MIT

**CLI Entry Points (from `pyproject.toml`):**
- `nim` → `nim_code:main`
- `nim-proxy` → `proxy:main`

**Dependency File:**
- `requirements.txt` — flat pinned requirements (includes both runtime and dev deps)
  - Runtime: `fastapi`, `uvicorn[standard]`, `httpx[http2]`, `orjson`, `PyYAML`
  - Dev/Test: `pytest`, `pytest-asyncio`, `respx`, `pytest-cov`

## Dev Tools & Linting

**Test Runner:**
- `pytest>=8.0` — test framework
- `pytest-asyncio>=0.23` — async test support
- `pytest-cov>=5.0` — coverage reporting
- `respx>=0.21` — HTTPX request mocking for tests
- Test files: `tests/test_translation.py`, `tests/test_e2e.py`

**No linter/formatter detected:**
- No `.eslintrc`, `biome.json`, `ruff.toml`, `.flake8`, or `mypy.ini` present
- No pre-commit hooks detected

## Infrastructure / Deployment

**Deployment Model:**
- Single-file Python process (`proxy.py`) run directly with `python proxy.py` or via `nim-proxy` CLI
- Listens on `127.0.0.1:8787` by default (local-only; not internet-facing by default)
- No containerization config detected (no `Dockerfile` or `docker-compose.yml`)
- No cloud deployment config detected (no `Procfile`, `fly.toml`, etc.)

**Distribution:**
- Wheel and sdist via hatch: `hatch build`
- Distributed as installable Python package (`nvd-claude-nim`)

**Configuration Sources (priority order):**
1. Environment variables (highest priority)
2. `config.yaml` (path via `PROXY_CONFIG` env var, defaults to `./config.yaml`)
3. Hardcoded defaults in `proxy.py`

---

*Stack analysis: 2026-05-13*
