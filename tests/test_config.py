import pytest

from deepsee.config.loader import load_config
from deepsee.errors import ConfigError


def test_env_only_config(monkeypatch):
    env = {
        "DEEPSEEK_API_KEY": "sk-ds-1",
        "VISION_API_KEY": "sk-vision-1",
        "VISION_BASE_URL": "https://vision.example.com/v1",
    }
    cfg = load_config(env=env)
    assert cfg.deepseek.api_key == "sk-ds-1"
    assert cfg.deepseek.base_url == "https://api.deepseek.com"
    assert cfg.deepseek.model == "deepseek-chat"
    assert cfg.vision.backend == "openai_compatible"  # default
    assert cfg.vision.model == ""
    assert cfg.vision.base_url == "https://vision.example.com/v1"
    assert cfg.retries == 2


def test_toml_with_env_expansion(tmp_path, monkeypatch):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "${MY_DS_KEY}"\n'
        'model = "deepseek-reasoner"\n'
        "[vision]\n"
        'backend = "gemini"\n'
        'api_key = "${MY_GEMINI_KEY}"\n'
        'model = "gemini-2.0-flash"\n'
    )
    env = {"MY_DS_KEY": "sk-ds-2", "MY_GEMINI_KEY": "sk-gem-2"}
    cfg = load_config(path=toml, env=env)
    assert cfg.deepseek.api_key == "sk-ds-2"
    assert cfg.deepseek.model == "deepseek-reasoner"
    assert cfg.vision.backend == "gemini"
    assert cfg.vision.base_url == "https://generativelanguage.googleapis.com"
    assert cfg.vision.model == "gemini-2.0-flash"


def test_env_overrides_toml(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "toml-key"\n'
        "[vision]\n"
        'backend = "anthropic"\n'
        'api_key = "toml-vision"\n'
        'model = "claude-sonnet-4-5"\n'
    )
    env = {"DEEPSEEK_API_KEY": "env-key"}
    cfg = load_config(path=toml, env=env)
    assert cfg.deepseek.api_key == "env-key"
    assert cfg.vision.api_key == "toml-vision"
    assert cfg.vision.backend == "anthropic"
    assert cfg.vision.base_url == "https://api.anthropic.com"


def test_missing_env_reference_raises(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "${DOES_NOT_EXIST_123}"\n'
        "[vision]\n"
        'backend = "openai_compatible"\n'
        'api_key = "x"\n'
        'base_url = "https://example.com/v1"\n'
    )
    with pytest.raises(ConfigError):
        load_config(path=toml, env={})


def test_missing_deepseek_key_raises():
    env = {"VISION_API_KEY": "sk-vision-1"}
    with pytest.raises(ConfigError, match="deepseek.api_key"):
        load_config(env=env)


def test_missing_vision_key_raises():
    env = {"DEEPSEEK_API_KEY": "sk-ds-1"}
    with pytest.raises(ConfigError, match="vision.api_key"):
        load_config(env=env)


def test_invalid_backend_raises(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "x"\n'
        "[vision]\n"
        'backend = "openai"\n'
        'api_key = "y"\n'
    )
    with pytest.raises(ConfigError, match="backend"):
        load_config(path=toml, env={})


def test_openai_compatible_requires_base_url(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "x"\n'
        "[vision]\n"
        'backend = "openai_compatible"\n'
        'api_key = "y"\n'
    )
    with pytest.raises(ConfigError, match="base_url"):
        load_config(path=toml, env={})


def test_invalid_base_url_raises(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "x"\n'
        'base_url = "not-a-url"\n'
        "[vision]\n"
        'backend = "anthropic"\n'
        'api_key = "y"\n'
    )
    with pytest.raises(ConfigError, match="base_url"):
        load_config(path=toml, env={})


def test_retries_env_override(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "x"\n'
        "retries = 5\n"
        "[vision]\n"
        'backend = "anthropic"\n'
        'api_key = "y"\n'
    )
    cfg = load_config(path=toml, env={"DeepSee_RETRIES": "7"})
    assert cfg.retries == 7