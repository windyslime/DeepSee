import pytest

from deepsee_server.config import DEFAULT_HOST, DEFAULT_PORT, server_settings


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
