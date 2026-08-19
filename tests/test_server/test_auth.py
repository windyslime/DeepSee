import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from deepsee_server.app import app, configure_request_guard
from deepsee_server.auth import (
    ApiKeyStore,
    configure_api_key_store,
    disable_api_key_auth,
)


@pytest.fixture(autouse=True)
def reset_gateway_security_state():
    configure_api_key_store(None)
    configure_request_guard(None)
    yield
    configure_api_key_store(None)
    configure_request_guard(None)


@pytest.fixture
def configured_store(tmp_path):
    store = ApiKeyStore(tmp_path / "api-keys.json")
    public = store.create("public", "test public")
    admin = store.create("admin", "test admin")
    configure_api_key_store(store)
    return store, public, admin


def test_direct_import_fails_closed_but_health_is_public():
    client = TestClient(app)

    health = client.get("/health")
    protected = client.get("/v1/models")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert len(health.json()["instanceId"]) == 32
    assert protected.status_code == 503
    assert protected.json()["error"]["type"] == "configuration_error"


def test_key_store_persists_only_digests_and_scopes_are_isolated(tmp_path):
    path = tmp_path / "api-keys.json"
    store = ApiKeyStore(path)
    public = store.create("public", "desktop")
    admin = store.create("admin", "operator")

    persisted = path.read_text(encoding="utf-8")
    assert public.key not in persisted
    assert admin.key not in persisted
    assert "digest" in persisted
    assert store.validate(public.key, "public") is True
    assert store.validate(public.key, "admin") is False
    assert store.validate(admin.key, "admin") is True
    assert store.validate(admin.key, "public") is False
    assert os.stat(path).st_mode & 0o777 == 0o600

    reloaded = ApiKeyStore(path)
    assert reloaded.validate(public.key, "public") is True
    assert reloaded.revoke(public.id) is True
    assert reloaded.validate(public.key, "public") is False
    assert reloaded.revoke("does-not-exist") is False


def test_concurrent_key_creation_preserves_every_record_and_atomic_file(tmp_path):
    store = ApiKeyStore(tmp_path / "api-keys.json")

    with ThreadPoolExecutor(max_workers=8) as executor:
        created = list(
            executor.map(
                lambda index: store.create("public", f"client-{index}"), range(32)
            )
        )

    assert len(store.list()) == 32
    assert all(store.validate(key.key, "public") for key in created)
    assert os.stat(store.path).st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".api-keys.json.*"))


def test_authentication_scope_and_admin_key_management(configured_store, monkeypatch):
    store, public, admin = configured_store
    client = TestClient(app)
    monkeypatch.setattr(
        "deepsee_server.app._current_config",
        lambda: type(
            "Config",
            (), {"deepseek": type("DeepSeek", (), {"model": "test-model"})()},
        )(),
    )

    assert client.get("/v1/models").status_code == 401
    assert (
        client.get(
            "/v1/models", headers={"Authorization": f"Bearer {admin.key}"}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/admin/keys", headers={"X-DeepSee-Admin-Key": public.key}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/admin/keys", headers={"Authorization": f"Bearer {public.key}"}
        ).status_code
        == 401
    )

    public_response = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {public.key}"}
    )
    assert public_response.status_code == 200
    assert public_response.json()["data"][0]["id"] == "test-model"

    listed = client.get(
        "/admin/keys", headers={"X-DeepSee-Admin-Key": admin.key}
    )
    assert listed.status_code == 200
    assert public.key not in json.dumps(listed.json())
    assert admin.key not in json.dumps(listed.json())

    invalid = client.post(
        "/admin/keys",
        headers={"X-DeepSee-Admin-Key": admin.key},
        json={"scope": "public", "label": ""},
    )
    assert invalid.status_code == 400

    created = client.post(
        "/admin/keys",
        headers={"X-DeepSee-Admin-Key": admin.key},
        json={"scope": "public", "label": "CLI"},
    )
    assert created.status_code == 200
    created_body = created.json()
    assert store.validate(created_body["key"], "public") is True
    assert created_body["key"] not in json.dumps(store.list())

    revoked = client.delete(
        f"/admin/keys/{created_body['id']}",
        headers={"X-DeepSee-Admin-Key": admin.key},
    )
    assert revoked.status_code == 200
    assert store.validate(created_body["key"], "public") is False


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/v1/models", None),
        ("post", "/v1/chat/completions", b"not json"),
        ("post", "/v1/dsv", b"not json"),
        ("post", "/v1/messages", b"not json"),
        ("post", "/v1beta/models/test:generateContent", b"not json"),
        ("post", "/analyze", b"not json"),
    ],
)
def test_missing_public_key_is_rejected_before_config_or_body_parsing(
    configured_store, monkeypatch, method, path, body
):
    calls = []

    def config_must_not_load():
        calls.append("config")
        raise AssertionError("authentication must run before configuration loading")

    async def body_must_not_parse(_request):
        calls.append("body")
        raise AssertionError("authentication must run before body parsing")

    monkeypatch.setattr("deepsee_server.app._current_config", config_must_not_load)
    monkeypatch.setattr("deepsee_server.app._read_body_limited", body_must_not_parse)
    client = TestClient(app)
    request = getattr(client, method)
    response = request(path, content=body) if body is not None else request(path)

    assert response.status_code == 401
    assert response.json()["error"]["type"] == "authentication_error"
    assert calls == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/chat/completions", b"not json"),
        ("/v1/dsv", b"not json"),
        ("/v1/messages", b"not json"),
        ("/v1beta/models/test:generateContent", b"not json"),
        ("/analyze", b"not json"),
    ],
)
def test_public_key_opens_every_protected_post_protocol_to_existing_validation(
    configured_store, path, body
):
    _, public, _ = configured_store
    response = TestClient(app).post(
        path,
        headers={"Authorization": f"Bearer {public.key}"},
        content=body,
    )

    assert response.status_code == 400


def test_explicit_no_auth_mode_marks_legacy_inference_requests_as_permitted(monkeypatch):
    disable_api_key_auth()
    monkeypatch.setattr(
        "deepsee_server.app._current_config",
        lambda: type(
            "Config",
            (), {"deepseek": type("DeepSeek", (), {"model": "test-model"})()},
        )(),
    )

    response = TestClient(app).get("/v1/models")

    assert response.status_code == 200


def test_rejected_authentication_never_calls_upstream(configured_store, monkeypatch):
    calls = []

    async def ask_must_not_run(*args, **kwargs):
        calls.append("upstream")
        raise AssertionError("authentication must run before upstream inference")

    monkeypatch.setattr("deepsee_server.app.ask_async", ask_must_not_run)
    response = TestClient(app).post(
        "/v1/chat/completions",
        content=b"not json",
    )

    assert response.status_code == 401
    assert calls == []


def test_configured_authentication_is_not_bypassed_by_preflight(
    configured_store, monkeypatch
):
    _, public, _ = configured_store
    monkeypatch.setattr(
        "deepsee_server.app._current_config",
        lambda: type(
            "Config",
            (), {"deepseek": type("DeepSeek", (), {"model": "test-model"})()},
        )(),
    )
    client = TestClient(app)

    preflight = client.options(
        "/v1/chat/completions",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "POST",
        },
    )
    protected = client.get(
        "/v1/models", headers={"Authorization": f"Bearer {public.key}"}
    )

    assert preflight.status_code in (200, 405)
    assert protected.status_code == 200


def test_corrupted_key_file_maps_to_503_not_500(tmp_path):
    """损坏的 key 文件(非法 JSON)不得让受保护路由裸 500。"""
    from deepsee_server.auth import ApiKeyStoreError

    keys_file = tmp_path / "api-keys.json"
    keys_file.write_text("{not json", encoding="utf-8")
    store = ApiKeyStore(keys_file)
    configure_api_key_store(store)
    client = TestClient(app)

    with pytest.raises(ApiKeyStoreError):
        store.check()
    response = client.get("/v1/models", headers={"Authorization": "Bearer anything"})

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "configuration_error"


def test_key_file_wrong_schema_maps_to_503_not_500(tmp_path):
    """key 文件结构错误(顶层非数组)同样 fail closed 为 503。"""
    keys_file = tmp_path / "api-keys.json"
    keys_file.write_text('{"keys": []}', encoding="utf-8")
    store = ApiKeyStore(keys_file)
    configure_api_key_store(store)
    client = TestClient(app)

    response = client.get("/v1/models", headers={"Authorization": "Bearer anything"})

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "configuration_error"


def test_key_file_record_missing_fields_maps_to_503_not_500(tmp_path):
    """记录缺少必填字段视为损坏,受保护路由返回 503。"""
    keys_file = tmp_path / "api-keys.json"
    keys_file.write_text(
        json.dumps([{"id": "x", "digest": "y"}]),
        encoding="utf-8",
    )
    store = ApiKeyStore(keys_file)
    configure_api_key_store(store)
    client = TestClient(app)

    response = client.get("/v1/models", headers={"Authorization": "Bearer anything"})

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "configuration_error"


def test_admin_routes_map_key_store_errors_to_503(tmp_path):
    """admin 端点(list/create/revoke)对损坏存储同样返回 503。"""
    keys_file = tmp_path / "api-keys.json"
    keys_file.write_text("{not json", encoding="utf-8")
    store = ApiKeyStore(keys_file)
    configure_api_key_store(store)
    client = TestClient(app)

    assert client.get("/admin/keys").status_code == 503
    assert client.post("/admin/keys", json={"scope": "public", "label": "x"}).status_code == 503
    assert client.delete("/admin/keys/some-id").status_code == 503
