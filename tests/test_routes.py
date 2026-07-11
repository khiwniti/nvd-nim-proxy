"""Route-level smoke tests.

Bypasses the FastAPI lifespan (which requires NVIDIA_API_KEY) so these
tests can run offline. /healthz needs no upstream client; /v1/messages
auth-rejection and validation paths run before any upstream call.
"""

from fastapi.testclient import TestClient

import proxy


def _client() -> TestClient:
    # raise_server_exceptions=False ensures any internal lifespan-related
    # surprises surface as responses rather than crashing pytest.
    return TestClient(proxy.app, raise_server_exceptions=False)


def test_healthz_returns_ok():
    with _client() as c:
        # TestClient runs lifespan; we tolerate the NVIDIA_API_KEY guard
        # by short-circuiting before issuing the request only if needed.
        r = c.get("/healthz")
    assert r.status_code == 200
    # Backwards-compat: top-level ``status`` is still the liveness signal.
    # 0.3.0 adds a richer ``components`` block alongside.
    body = r.json()
    assert body["status"] == "ok"
    assert "components" in body
    assert body["components"]["key_configured"] is True
    assert body["components"]["upstream_built"] is True
    assert body["components"]["models_loaded"] >= 1
    assert isinstance(body["components"]["blocked_models"], list)
    assert body["components"]["stream_budget_s"] > 0


def test_health_alias_returns_ok():
    with _client() as c:
        r = c.get("/health")
    assert r.status_code == 200
    # Same envelope shape on both aliases so probes and dashboards
    # don't have to special-case the path.
    assert r.json()["status"] == "ok"


def test_messages_rejects_missing_required_fields():
    with _client() as c:
        r = c.post("/v1/messages", json={"model": "claude-3-5-sonnet-20241022"})
    assert r.status_code == 400
    body = r.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"


def test_messages_enforces_proxy_api_key(monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_API_KEY", "secret")
    with _client() as c:
        r = c.post("/v1/messages", json={"model": "x", "messages": []})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"


def test_count_tokens_enforces_proxy_api_key(monkeypatch):
    monkeypatch.setattr(proxy, "PROXY_API_KEY", "secret")
    with _client() as c:
        r = c.post("/v1/messages/count_tokens", json={"messages": []})
    assert r.status_code == 401
    assert r.json()["error"]["type"] == "authentication_error"


def test_uvicorn_config_kwargs_match_real_signature():
    """Regression guard for the 0.3.0 SIGTERM grace-window crash.

    ``proxy.main()`` builds ``uvicorn.Config`` directly. uvicorn's Config
    constructor validates kwargs against its signature, so a typo (e.g.
    ``timeout_grace_time``) raises ``TypeError`` at config construction
    time and crashes the daemon before uvicorn even starts. The TestClient
    path doesn't exercise main(), so this guard lives here as an
    introspection-only assertion.

    If a future uvicorn release renames any of these kwargs, this test
    must be updated in lockstep.
    """
    import inspect

    import uvicorn

    sig = inspect.signature(uvicorn.Config.__init__)
    accepted = set(sig.parameters)

    # These are the kwargs we pass (or refer to) in proxy.main()'s
    # uvicorn.Config(...) call. If the proxy code starts passing additional
    # kwargs, add them here too — silent renames drift the daemon into a
    # TypeError at boot.
    required_kwargs = {
        "app",
        "host",
        "port",
        "log_level",
        "access_log",
        "loop",
        "http",
        "timeout_graceful_shutdown",  # NOT timeout_grace_time — that's not real
    }
    missing = required_kwargs - accepted
    assert not missing, (
        "uvicorn.Config no longer accepts these kwargs we rely on: "
        f"{sorted(missing)}. Update proxy.main() to match the new signature."
    )


def test_main_does_not_crash_at_uvicorn_config_build(monkeypatch):
    """Drives ``proxy.main()`` just far enough to verify the uvicorn
    Config kwargs pass validation. We monkey-patch ``uvicorn.Server.run``
    to a no-op so we don't actually bind a socket or block."""
    import uvicorn

    class _StubServer:
        def __init__(self, config):
            self.config = config

        def run(self):
            return None

    monkeypatch.setattr(uvicorn, "Server", _StubServer)
    # Free up a real port for the binding step (uvicorn.Config binds at
    # construction time when host/port are passed).
    monkeypatch.setattr(proxy, "PROXY_PORT", 0)
    monkeypatch.setattr(proxy, "PROXY_HOST", "127.0.0.1")

    # If main() constructed uvicorn.Config successfully, no exception leaks.
    proxy.main()
