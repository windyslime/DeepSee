import json

import pytest
from fastapi.testclient import TestClient

from deepsee_server.app import app, mount_web_dist
from deepsee_server.auth import (
    ApiKeyStore,
    configure_api_key_store,
    disable_api_key_auth,
)
from deepsee_server.traces import RequestTrace, TraceStore, request_traces


@pytest.fixture(autouse=True)
def reset_gateway_state():
    configure_api_key_store(None)
    request_traces.clear()
    yield
    configure_api_key_store(None)
    request_traces.clear()


def test_health_is_available_without_authentication():
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert len(response.json()["instanceId"]) == 32


def test_cors_allows_vite_development_origins_only():
    client = TestClient(app)
    allowed = client.options(
        "/v1/chat/completions",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    denied = client.options(
        "/v1/chat/completions",
        headers={
            "Origin": "https://attacker.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


def test_cors_is_present_on_authentication_errors(tmp_path):
    store = ApiKeyStore(tmp_path / "keys.json")
    store.create("public", "test client")
    configure_api_key_store(store)

    response = TestClient(app).get(
        "/v1/models",
        headers={"Origin": "http://127.0.0.1:5173"},
    )

    assert response.status_code == 401
    assert (
        response.headers["access-control-allow-origin"]
        == "http://127.0.0.1:5173"
    )


def test_admin_traces_require_an_admin_key(tmp_path):
    store = ApiKeyStore(tmp_path / "keys.json")
    public = store.create("public", "test client")
    admin = store.create("admin", "test admin")
    configure_api_key_store(store)
    client = TestClient(app)

    assert client.get("/admin/traces").status_code == 401
    assert (
        client.get(
            "/admin/traces",
            headers={"Authorization": f"Bearer {public.key}"},
        ).status_code
        == 401
    )
    response = client.get(
        "/admin/traces",
        headers={"X-DeepSee-Admin-Key": admin.key},
    )

    assert response.status_code == 200
    assert "api_key" not in json.dumps(response.json())


def test_request_traces_capture_metadata_without_request_content():
    disable_api_key_auth()
    client = TestClient(app)

    response = client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "private prompt"}]},
    )

    traces = request_traces.list()
    assert response.status_code >= 400
    assert traces[0]["path"] == "/v1/chat/completions"
    serialized = json.dumps(traces)
    assert "private prompt" not in serialized
    assert "authorization" not in serialized.lower()


def test_trace_store_evicts_oldest_entries():
    store = TraceStore(max_entries=2)

    for index in range(3):
        store.append(
            RequestTrace(
                id=str(index),
                method="GET",
                path="/health",
                status=200,
                latency_ms=index,
            )
        )

    assert [trace["id"] for trace in store.list()] == ["2", "1"]


def test_mount_web_dist_requires_an_index_file(tmp_path):
    with pytest.raises(ValueError, match="index.html"):
        mount_web_dist(tmp_path)


def test_mount_web_dist_serves_site_without_shadowing_api_routes(tmp_path):
    (tmp_path / "index.html").write_text(
        "<!doctype html><title>DeepSee Desktop</title>",
        encoding="utf-8",
    )

    original_routes = list(app.routes)
    try:
        mounted = mount_web_dist(tmp_path)
        client = TestClient(app)

        assert mounted == tmp_path.resolve()
        assert client.get("/").status_code == 200
        assert "DeepSee Desktop" in client.get("/").text
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        assert len(health.json()["instanceId"]) == 32
    finally:
        app.router.routes[:] = original_routes
