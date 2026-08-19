import pytest

from deepsee_server.config import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    GuardSettings,
    request_guard_settings,
    server_settings,
)
from deepsee_server.__main__ import validate_no_auth_host


@pytest.fixture(autouse=True)
def reset_auth_state():
    from deepsee_server.auth import configure_api_key_store

    configure_api_key_store(None)
    yield
    configure_api_key_store(None)


def test_default_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("DeepSee_SERVER_HOST", raising=False)
    monkeypatch.delenv("DeepSee_SERVER_PORT", raising=False)
    monkeypatch.chdir(tmp_path)  # 无 deepsee.toml
    s = server_settings()
    assert s.host == DEFAULT_HOST == "127.0.0.1"
    assert s.port == DEFAULT_PORT == 8712


def test_toml_server_section(tmp_path, monkeypatch):
    toml = tmp_path / "deepsee.toml"
    toml.write_text('[server]\nhost = "0.0.0.0"\nport = 9000\n')
    monkeypatch.chdir(tmp_path)
    s = server_settings()
    assert s.host == "0.0.0.0"
    assert s.port == 9000


def test_env_overrides_toml(tmp_path, monkeypatch):
    toml = tmp_path / "deepsee.toml"
    toml.write_text('[server]\nport = 9000\n')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DeepSee_SERVER_PORT", "9999")
    monkeypatch.setenv("DeepSee_SERVER_HOST", "0.0.0.0")
    s = server_settings()
    assert s.port == 9999
    assert s.host == "0.0.0.0"


def test_invalid_port_raises(tmp_path, monkeypatch):
    toml = tmp_path / "deepsee.toml"
    toml.write_text('[server]\nport = "not-a-number"\n')
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="port"):
        server_settings()


def test_default_request_guard_settings(monkeypatch):
    for name in (
        "DeepSee_MAX_CONCURRENT_REQUESTS",
        "DeepSee_REQUEST_QUEUE_TIMEOUT",
        "DeepSee_RATE_LIMIT_REQUESTS",
        "DeepSee_RATE_LIMIT_WINDOW",
    ):
        monkeypatch.delenv(name, raising=False)

    assert request_guard_settings() == GuardSettings(
        max_concurrent=8,
        queue_timeout=2.0,
        rate_limit=60,
        rate_window=60.0,
    )


def test_request_guard_settings_accept_environment_overrides(monkeypatch):
    monkeypatch.setenv("DeepSee_MAX_CONCURRENT_REQUESTS", "3")
    monkeypatch.setenv("DeepSee_REQUEST_QUEUE_TIMEOUT", "0.5")
    monkeypatch.setenv("DeepSee_RATE_LIMIT_REQUESTS", "7")
    monkeypatch.setenv("DeepSee_RATE_LIMIT_WINDOW", "15.25")

    assert request_guard_settings() == GuardSettings(
        max_concurrent=3,
        queue_timeout=0.5,
        rate_limit=7,
        rate_window=15.25,
    )


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DeepSee_MAX_CONCURRENT_REQUESTS", "0"),
        ("DeepSee_MAX_CONCURRENT_REQUESTS", "-1"),
        ("DeepSee_MAX_CONCURRENT_REQUESTS", "none"),
        ("DeepSee_REQUEST_QUEUE_TIMEOUT", "0"),
        ("DeepSee_REQUEST_QUEUE_TIMEOUT", "-0.1"),
        ("DeepSee_REQUEST_QUEUE_TIMEOUT", "NaN"),
        ("DeepSee_REQUEST_QUEUE_TIMEOUT", "inf"),
        ("DeepSee_RATE_LIMIT_REQUESTS", "0"),
        ("DeepSee_RATE_LIMIT_REQUESTS", "-2"),
        ("DeepSee_RATE_LIMIT_REQUESTS", "not-an-int"),
        ("DeepSee_RATE_LIMIT_WINDOW", "NaN"),
        ("DeepSee_RATE_LIMIT_WINDOW", "inf"),
        ("DeepSee_RATE_LIMIT_WINDOW", "nope"),
    ],
)
def test_invalid_request_guard_environment_values_raise(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        request_guard_settings()


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_no_auth_accepts_only_loopback_hosts(host):
    assert validate_no_auth_host(host) is None


@pytest.mark.parametrize("host", ["", "0.0.0.0", "192.168.1.10", "[::1]", "localhost."])
def test_no_auth_rejects_non_loopback_hosts(host):
    with pytest.raises(ValueError, match="loopback"):
        validate_no_auth_host(host)


def test_invalid_no_auth_host_has_no_key_or_server_side_effects(monkeypatch):
    import deepsee_server.__main__ as server_main

    calls = []

    def keys_must_not_be_touched(*args, **kwargs):
        calls.append("keys")
        raise AssertionError("host validation must precede key-file access")

    def server_must_not_start(*args, **kwargs):
        calls.append("uvicorn")
        raise AssertionError("host validation must precede uvicorn startup")

    monkeypatch.setattr(server_main, "ApiKeyStore", keys_must_not_be_touched)
    monkeypatch.setattr(server_main.uvicorn, "run", server_must_not_start)
    monkeypatch.setattr("sys.argv", ["deepsee-server", "--no-auth", "--host", "0.0.0.0"])

    with pytest.raises(ValueError, match="loopback"):
        server_main.main()
    assert calls == []


def test_cli_creates_default_keys_once_and_recovery_keys_on_demand(
    tmp_path, monkeypatch, capsys
):
    import deepsee_server.__main__ as server_main
    from deepsee_server.auth import ApiKeyStore

    key_path = tmp_path / "api-keys.json"
    starts = []
    monkeypatch.setattr(
        server_main.uvicorn,
        "run",
        lambda application, host, port: starts.append((application, host, port)),
    )

    monkeypatch.setattr(
        "sys.argv",
        ["deepsee-server", "--keys-file", str(key_path), "--host", "127.0.0.1"],
    )
    server_main.main()
    first_output = capsys.readouterr().out
    first_records = ApiKeyStore(key_path).list()

    assert len(first_records) == 2
    assert {record["scope"] for record in first_records} == {"public", "admin"}
    assert {record["label"] for record in first_records} == {
        "default-public",
        "default-admin",
    }
    assert "Public API key:" in first_output
    assert "Admin API key:" in first_output
    assert starts == [(server_main.app, "127.0.0.1", 8712)]

    server_main.main()
    restart_output = capsys.readouterr().out
    assert len(ApiKeyStore(key_path).list()) == 2
    assert "Public API key:" not in restart_output
    assert "Admin API key:" not in restart_output

    monkeypatch.setattr(
        "sys.argv",
        [
            "deepsee-server",
            "--keys-file",
            str(key_path),
            "--host",
            "127.0.0.1",
            "--create-recovery-keys",
        ],
    )
    server_main.main()
    recovery_output = capsys.readouterr().out
    recovery_records = ApiKeyStore(key_path).list()

    assert len(recovery_records) == 4
    assert "Public API key:" in recovery_output
    assert "Admin API key:" in recovery_output
