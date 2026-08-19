import json

import pytest
from fastapi.testclient import TestClient

from deepsee_server.app import app, configure_upstream_store
from deepsee_server.auth import configure_api_key_store, disable_api_key_auth
from deepsee_server.runtime_control import (
    RestartController,
    configure_restart_controller,
    configured_restart_controller,
)
from deepsee_server.upstream_config import (
    ManagedProviderConfig,
    ManagedUpstreamConfig,
    UpstreamConfigStore,
)
import deepsee_server.app as app_module


@pytest.fixture
def admin_client(tmp_path):
    previous_store = app_module._upstream_store
    previous_restart_controller = configured_restart_controller()
    disable_api_key_auth()
    store = UpstreamConfigStore(tmp_path / "upstream.json")
    store.save(
        ManagedUpstreamConfig(
            deepseek=ManagedProviderConfig(
                "deepseek-secret", "https://api.deepseek.com", "deepseek-chat"
            ),
            vision=ManagedProviderConfig(
                "vision-secret", "https://vision.example/v1", "vision-model"
            ),
        )
    )
    configure_upstream_store(store)
    configure_restart_controller(RestartController(enabled=False))
    try:
        yield TestClient(app)
    finally:
        configure_upstream_store(previous_store)
        configure_restart_controller(previous_restart_controller)
        configure_api_key_store(None)


def test_health_exposes_a_stable_process_instance_id():
    client = TestClient(app)

    first = client.get("/health").json()
    second = client.get("/health").json()

    assert first["status"] == "ok"
    assert len(first["instanceId"]) == 32
    assert second["instanceId"] == first["instanceId"]


def test_admin_config_returns_redacted_effective_configuration(admin_client):
    response = admin_client.get("/admin/config")
    serialized = json.dumps(response.json())

    assert response.status_code == 200
    assert response.json()["deepseek"] == {
        "baseUrl": "https://api.deepseek.com",
        "baseUrlWritable": True,
        "model": "deepseek-chat",
        "modelWritable": True,
        "keyConfigured": True,
        "keySource": "managed",
        "keyWritable": True,
    }
    assert response.json()["vision"]["model"] == "vision-model"
    assert response.json()["restartSupported"] is False
    assert "deepseek-secret" not in serialized
    assert "vision-secret" not in serialized


def test_admin_config_saves_key_mutations_and_schedules_supported_restart(tmp_path):
    disable_api_key_auth()
    store = UpstreamConfigStore(tmp_path / "upstream.json")
    store.save(
        ManagedUpstreamConfig(
            deepseek=ManagedProviderConfig(
                "deepseek-secret", "https://api.deepseek.com", "deepseek-chat"
            ),
            vision=ManagedProviderConfig(
                "old-vision-secret", "https://old-vision.example/v1", "old-vision"
            ),
        )
    )
    scheduled = []
    configure_upstream_store(store)
    configure_restart_controller(
        RestartController(
            enabled=True,
            environment={"XPC_SERVICE_NAME": "com.deepsee.gateway"},
            schedule=lambda delay, callback: scheduled.append((delay, callback)),
            terminate=lambda: None,
        )
    )

    response = TestClient(app).post(
        "/admin/config",
        json={
            "deepseek": {
                "baseUrl": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "key": {"action": "keep"},
            },
            "vision": {
                "backend": "openai_compatible",
                "baseUrl": "https://vision.example/v1",
                "model": "vision-model",
                "key": {"action": "replace", "value": "new-vision-secret"},
            },
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "saved": True,
        "restartRequired": True,
        "restartSupported": True,
    }
    saved = store.load()
    assert saved is not None
    assert saved.deepseek.api_key == "deepseek-secret"
    assert saved.vision.api_key == "new-vision-secret"
    assert len(scheduled) == 1


def test_admin_config_verify_returns_both_independent_provider_results(
    admin_client, monkeypatch
):
    expected = {
        "deepseek": {"ok": True, "latencyMs": 12},
        "vision": {
            "ok": False,
            "latencyMs": 18,
            "error": {"code": "AUTH", "message": "认证失败"},
        },
    }

    async def fake_verify(_config):
        return expected

    monkeypatch.setattr(app_module, "verify_upstream_connections", fake_verify)

    response = admin_client.post("/admin/config/verify")

    assert response.status_code == 200
    assert response.json() == expected


def test_admin_config_rejects_writes_shadowed_by_environment(
    admin_client, monkeypatch
):
    monkeypatch.setenv("DeepSee_VISION_API_KEY", "environment-vision-secret")

    response = admin_client.post(
        "/admin/config",
        json={
            "deepseek": {
                "baseUrl": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "key": {"action": "keep"},
            },
            "vision": {
                "backend": "openai_compatible",
                "baseUrl": "https://vision.example/v1",
                "model": "vision-model",
                "key": {"action": "replace", "value": "browser-vision-secret"},
            },
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "configuration_conflict"
    assert "environment-vision-secret" not in json.dumps(response.json())
    assert "browser-vision-secret" not in json.dumps(response.json())


def test_admin_config_can_keep_environment_credentials_without_copying_them(
    tmp_path, monkeypatch
):
    disable_api_key_auth()
    monkeypatch.setenv("DeepSee_DEEPSEEK_API_KEY", "environment-deepseek")
    monkeypatch.setenv("DeepSee_VISION_API_KEY", "environment-vision")
    store = UpstreamConfigStore(tmp_path / "upstream.json")
    configure_upstream_store(store)
    configure_restart_controller(RestartController(enabled=False))

    response = TestClient(app).post(
        "/admin/config",
        json={
            "deepseek": {
                "baseUrl": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "key": {"action": "keep"},
            },
            "vision": {
                "backend": "openai_compatible",
                "baseUrl": "https://vision.example/v1",
                "model": "vision-model",
                "key": {"action": "keep"},
            },
        },
    )

    assert response.status_code == 200
    saved = store.load()
    assert saved is not None
    assert saved.deepseek.api_key is None
    assert saved.vision.api_key is None


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"deepseek": {}, "vision": {}},
        {
            "deepseek": {
                "baseUrl": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "key": {"action": "unknown"},
            },
            "vision": {
                "backend": "openai_compatible",
                "baseUrl": "https://vision.example/v1",
                "model": "vision-model",
                "key": {"action": "keep"},
            },
        },
    ],
)
def test_admin_config_rejects_malformed_requests_without_replacing_file(
    admin_client, payload
):
    store = app_module._upstream_store
    before = store.path.read_bytes()

    response = admin_client.post("/admin/config", json=payload)

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert store.path.read_bytes() == before


def test_admin_config_rejects_unknown_fields(admin_client):
    response = admin_client.post(
        "/admin/config",
        json={
            "deepseek": {
                "baseUrl": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "key": {"action": "keep"},
                "unexpected": True,
            },
            "vision": {
                "backend": "openai_compatible",
                "baseUrl": "https://vision.example/v1",
                "model": "vision-model",
                "key": {"action": "keep"},
            },
        },
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_admin_config_maps_atomic_write_failure_without_replacing_file(
    admin_client, monkeypatch
):
    store = app_module._upstream_store
    before = store.path.read_bytes()

    def fail_save(_candidate):
        raise OSError("private path must not reach the response")

    monkeypatch.setattr(store, "save", fail_save)
    response = admin_client.post(
        "/admin/config",
        json={
            "deepseek": {
                "baseUrl": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "key": {"action": "keep"},
            },
            "vision": {
                "backend": "openai_compatible",
                "baseUrl": "https://vision.example/v1",
                "model": "vision-model",
                "key": {"action": "keep"},
            },
        },
    )

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "message": "上游配置写入失败",
            "type": "configuration_write_error",
        }
    }
    assert "private path" not in json.dumps(response.json())
    assert store.path.read_bytes() == before


def test_admin_config_marks_environment_endpoint_fields_read_only(
    admin_client, monkeypatch
):
    monkeypatch.setenv("DeepSee_DEEPSEEK_BASE_URL", "https://env.example/v1")

    view = admin_client.get("/admin/config")

    assert view.status_code == 200
    assert view.json()["deepseek"]["baseUrl"] == "https://env.example/v1"
    assert view.json()["deepseek"]["baseUrlSource"] == "env"
    assert view.json()["deepseek"]["baseUrlWritable"] is False

    response = admin_client.post(
        "/admin/config",
        json={
            "deepseek": {
                "baseUrl": "https://browser.example/v1",
                "model": "deepseek-chat",
                "key": {"action": "keep"},
            },
            "vision": {
                "backend": "openai_compatible",
                "baseUrl": "https://vision.example/v1",
                "model": "vision-model",
                "key": {"action": "keep"},
            },
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "configuration_conflict"
    assert "env.example" not in json.dumps(response.json())


def test_admin_config_protects_environment_fields_when_keys_are_incomplete(
    admin_client, monkeypatch
):
    store = app_module._upstream_store
    store.save(
        ManagedUpstreamConfig(
            deepseek=ManagedProviderConfig(
                None, "https://managed.example/v1", "deepseek-chat"
            ),
            vision=ManagedProviderConfig(
                None, "https://vision.example/v1", "vision-model"
            ),
        )
    )
    monkeypatch.setenv("DeepSee_DEEPSEEK_BASE_URL", "https://env.example/v1")

    response = admin_client.post(
        "/admin/config",
        json={
            "deepseek": {
                "baseUrl": "https://browser.example/v1",
                "model": "deepseek-chat",
                "key": {"action": "replace", "value": "new-deepseek"},
            },
            "vision": {
                "backend": "openai_compatible",
                "baseUrl": "https://vision.example/v1",
                "model": "vision-model",
                "key": {"action": "replace", "value": "new-vision"},
            },
        },
    )

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "configuration_conflict"


def test_admin_config_can_remove_a_managed_credential(admin_client):
    response = admin_client.post(
        "/admin/config",
        json={
            "deepseek": {
                "baseUrl": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "key": {"action": "remove"},
            },
            "vision": {
                "backend": "openai_compatible",
                "baseUrl": "https://vision.example/v1",
                "model": "vision-model",
                "key": {"action": "keep"},
            },
        },
    )

    assert response.status_code == 200
    saved = app_module._upstream_store.load()
    assert saved is not None
    assert saved.deepseek.api_key is None
    assert saved.deepseek.api_key_inherited is False


def test_admin_config_can_keep_a_toml_credential_when_other_key_is_missing(
    admin_client, tmp_path, monkeypatch
):
    store = app_module._upstream_store
    store.path.unlink()
    (tmp_path / "deepsee.toml").write_text(
        """
[deepseek]
api_key = "toml-deepseek"
base_url = "https://toml-deepseek.example/v1"
model = "toml-deepseek-model"

[vision]
backend = "openai_compatible"
base_url = "https://toml-vision.example/v1"
model = "toml-vision-model"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    response = admin_client.post(
        "/admin/config",
        json={
            "deepseek": {
                "baseUrl": "https://toml-deepseek.example/v1",
                "model": "toml-deepseek-model",
                "key": {"action": "keep"},
            },
            "vision": {
                "backend": "openai_compatible",
                "baseUrl": "https://toml-vision.example/v1",
                "model": "toml-vision-model",
                "key": {"action": "replace", "value": "managed-vision"},
            },
        },
    )

    assert response.status_code == 200
    saved = store.load()
    assert saved is not None
    assert saved.deepseek.api_key_inherited is True
    effective = app_module._current_config()
    assert effective.deepseek.api_key == "toml-deepseek"
    assert effective.vision.api_key == "managed-vision"


def test_admin_config_maps_corrupt_managed_file_to_stable_read_error(admin_client):
    store = app_module._upstream_store
    store.path.write_text("{broken", encoding="utf-8")

    response = admin_client.get("/admin/config")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "message": "上游配置读取失败",
            "type": "configuration_read_error",
        }
    }
    assert str(store.path) not in json.dumps(response.json())


def test_models_maps_corrupt_managed_file_to_configuration_unavailable(admin_client):
    store = app_module._upstream_store
    store.path.write_text("{broken", encoding="utf-8")

    response = admin_client.get("/v1/models")

    assert response.status_code == 503
    assert response.json() == {
        "error": {
            "message": "服务配置不可用",
            "type": "configuration_error",
        }
    }
    assert str(store.path) not in json.dumps(response.json())
