import pytest

from deepsee.config.loader import load_config
from deepsee.errors import ConfigError


def test_env_only_config(monkeypatch):
    env = {
        "DEEPSEEK_API_KEY": "sk-ds-1",
        "VISION_API_KEY": "sk-vision-1",
        "VISION_BASE_URL": "https://vision.example.com/v1",
        "VISION_MODEL": "qwen-vl-max",
    }
    cfg = load_config(env=env)
    assert cfg.deepseek.api_key == "sk-ds-1"
    assert cfg.deepseek.base_url == "https://api.deepseek.com"
    assert cfg.deepseek.model == "deepseek-chat"
    assert cfg.vision.backend == "openai_compatible"  # default
    assert cfg.vision.model == "qwen-vl-max"
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


@pytest.mark.parametrize("section", ["deepseek", "vision"])
def test_config_section_must_be_table(tmp_path, section):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(f'{section} = "not-a-table"\n')
    with pytest.raises(ConfigError, match=rf"{section} 必须是 TOML 表"):
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
        'model = "claude-sonnet-4-5"\n'
        'api_key = "y"\n'
    )
    cfg = load_config(path=toml, env={"DeepSee_RETRIES": "7"})
    assert cfg.retries == 7


def test_env_backend_switch_discards_old_toml_host(tmp_path):
    """Regression (security): env-switched backend must not keep the TOML
    host of the old backend, or the new API key would be sent to the
    wrong host."""
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "openai_compatible"\n'
        'api_key = "sk-old-host"\n'
        'base_url = "https://old-vision.example.com/v1"\n'
    )
    env = {
        "VISION_BACKEND": "anthropic",
        "VISION_API_KEY": "sk-anthropic-new",
        "VISION_MODEL": "claude-sonnet-4-5",
    }
    cfg = load_config(path=toml, env=env)
    assert cfg.vision.backend == "anthropic"
    assert cfg.vision.base_url == "https://api.anthropic.com"


def test_env_backend_switch_uses_new_backend_default_host(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "openai_compatible"\n'
        'api_key = "sk-old-host"\n'
    )
    env = {
        "VISION_BACKEND": "anthropic",
        "VISION_API_KEY": "sk-anthropic-new",
        "VISION_MODEL": "claude-sonnet-4-5",
    }
    cfg = load_config(path=toml, env=env)
    assert cfg.vision.base_url == "https://api.anthropic.com"


def test_env_backend_switch_respects_explicit_env_base_url(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "openai_compatible"\n'
        'api_key = "sk-old-host"\n'
        'base_url = "https://old-vision.example.com/v1"\n'
    )
    env = {
        "VISION_BACKEND": "anthropic",
        "VISION_API_KEY": "sk-anthropic-new",
        "VISION_BASE_URL": "https://anthropic-proxy.example.com",
        "VISION_MODEL": "claude-sonnet-4-5",
    }
    cfg = load_config(path=toml, env=env)
    assert cfg.vision.base_url == "https://anthropic-proxy.example.com"


def test_env_backend_switch_to_openai_compatible_requires_base_url(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "anthropic"\n'
        'api_key = "sk-anthropic"\n'
    )
    env = {
        "VISION_BACKEND": "openai_compatible",
        "VISION_API_KEY": "sk-new",
        "VISION_MODEL": "qwen-vl-max",
    }
    with pytest.raises(ConfigError, match="base_url"):
        load_config(path=toml, env=env)


def test_missing_vision_model_raises(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "anthropic"\n'
        'api_key = "sk-anthropic"\n'
    )
    with pytest.raises(ConfigError, match="vision.model"):
        load_config(path=toml, env={})


def test_env_backend_switch_requires_new_api_key(tmp_path):
    """Switching backend must not inherit the old provider's key."""
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "openai_compatible"\n'
        'api_key = "sk-old-provider"\n'
        'model = "qwen-vl"\n'
        'base_url = "https://old-vision.example.com/v1"\n'
    )
    env = {"VISION_BACKEND": "anthropic", "VISION_MODEL": "claude-sonnet-4-5"}
    with pytest.raises(ConfigError, match="VISION_API_KEY"):
        load_config(path=toml, env=env)


def test_env_backend_switch_requires_new_model(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "openai_compatible"\n'
        'api_key = "sk-old-provider"\n'
        'model = "qwen-vl"\n'
        'base_url = "https://old-vision.example.com/v1"\n'
    )
    env = {"VISION_BACKEND": "anthropic", "VISION_API_KEY": "sk-anthropic-new"}
    with pytest.raises(ConfigError, match="VISION_MODEL"):
        load_config(path=toml, env=env)


def test_env_backend_switch_uses_new_key_and_model(tmp_path):
    """P1 regression: after a switch, key/model must come from env — the old
    TOML provider's key and model are never inherited."""
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "openai_compatible"\n'
        'api_key = "sk-old-provider"\n'
        'model = "qwen-vl"\n'
        'base_url = "https://old-vision.example.com/v1"\n'
    )
    env = {
        "VISION_BACKEND": "anthropic",
        "VISION_API_KEY": "sk-anthropic-new",
        "VISION_MODEL": "claude-sonnet-4-5",
    }
    cfg = load_config(path=toml, env=env)
    assert cfg.vision.backend == "anthropic"
    assert cfg.vision.base_url == "https://api.anthropic.com"
    assert cfg.vision.api_key == "sk-anthropic-new"
    assert cfg.vision.model == "claude-sonnet-4-5"


def test_env_backend_switch_gemini_uses_default_host(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "openai_compatible"\n'
        'api_key = "sk-old-provider"\n'
        'model = "qwen-vl"\n'
        'base_url = "https://old-vision.example.com/v1"\n'
    )
    env = {
        "VISION_BACKEND": "gemini",
        "VISION_API_KEY": "sk-gemini-new",
        "VISION_MODEL": "gemini-2.0-flash",
    }
    cfg = load_config(path=toml, env=env)
    assert cfg.vision.base_url == "https://generativelanguage.googleapis.com"
    assert cfg.vision.api_key == "sk-gemini-new"
    assert cfg.vision.model == "gemini-2.0-flash"


def test_env_backend_switch_prefixed_env_vars(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "openai_compatible"\n'
        'api_key = "sk-old-provider"\n'
        'model = "qwen-vl"\n'
        'base_url = "https://old-vision.example.com/v1"\n'
    )
    env = {
        "DeepSee_VISION_BACKEND": "gemini",
        "DeepSee_VISION_API_KEY": "sk-gemini-prefixed",
        "DeepSee_VISION_MODEL": "gemini-2.5-pro",
    }
    cfg = load_config(path=toml, env=env)
    assert cfg.vision.backend == "gemini"
    assert cfg.vision.base_url == "https://generativelanguage.googleapis.com"
    assert cfg.vision.api_key == "sk-gemini-prefixed"
    assert cfg.vision.model == "gemini-2.5-pro"


def test_env_backend_same_value_keeps_toml_config(tmp_path):
    """Same-value VISION_BACKEND is not a switch: TOML base_url (custom
    proxy / audit / data residency) and key/model stay intact."""
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "anthropic"\n'
        'api_key = "sk-anthropic-toml"\n'
        'model = "claude-sonnet-4-5"\n'
        'base_url = "https://anthropic-proxy.example.com"\n'
    )
    cfg = load_config(path=toml, env={"VISION_BACKEND": "anthropic"})
    assert cfg.vision.base_url == "https://anthropic-proxy.example.com"
    assert cfg.vision.api_key == "sk-anthropic-toml"
    assert cfg.vision.model == "claude-sonnet-4-5"


def test_env_interpolated_backend_is_treated_as_switch(tmp_path):
    """P1: backend = "${VISION_BACKEND}" must not keep the old TOML host —
    the env-derived backend is a switch even though the expanded value
    equals the env value."""
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "${VISION_BACKEND}"\n'
        'api_key = "sk-old-provider"\n'
        'model = "qwen-vl"\n'
        'base_url = "https://old-vision.example.com/v1"\n'
    )
    env = {
        "VISION_BACKEND": "anthropic",
        "VISION_API_KEY": "sk-anthropic-new",
        "VISION_MODEL": "claude-sonnet-4-5",
    }
    cfg = load_config(path=toml, env=env)
    assert cfg.vision.backend == "anthropic"
    assert cfg.vision.base_url == "https://api.anthropic.com"
    assert cfg.vision.api_key == "sk-anthropic-new"
    assert cfg.vision.model == "claude-sonnet-4-5"


def test_env_interpolated_backend_requires_new_key(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "${VISION_BACKEND}"\n'
        'api_key = "sk-old-provider"\n'
        'model = "qwen-vl"\n'
        'base_url = "https://old-vision.example.com/v1"\n'
    )
    env = {"VISION_BACKEND": "anthropic", "VISION_MODEL": "claude-sonnet-4-5"}
    with pytest.raises(ConfigError, match="VISION_API_KEY"):
        load_config(path=toml, env=env)


def test_env_backend_switch_skips_old_env_placeholders(tmp_path):
    """P2: old ${OLD_*} placeholders must not be expanded when switching —
    loading must succeed with a complete new env config even if the old
    placeholders reference unset variables."""
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "${VISION_BACKEND}"\n'
        'api_key = "${OLD_PROVIDER_KEY}"\n'
        'model = "${OLD_PROVIDER_MODEL}"\n'
        'base_url = "https://old-vision.example.com/v1"\n'
    )
    env = {
        "VISION_BACKEND": "anthropic",
        "VISION_API_KEY": "sk-anthropic-new",
        "VISION_MODEL": "claude-sonnet-4-5",
    }
    cfg = load_config(path=toml, env=env)
    assert cfg.vision.api_key == "sk-anthropic-new"
    assert cfg.vision.model == "claude-sonnet-4-5"
    assert cfg.vision.base_url == "https://api.anthropic.com"


def test_env_backend_switch_invalid_backend_reported_first(tmp_path):
    """P3: an invalid backend is reported before missing key/model."""
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "openai_compatible"\n'
        'api_key = "sk-old-provider"\n'
        'model = "qwen-vl"\n'
        'base_url = "https://old-vision.example.com/v1"\n'
    )
    env = {"VISION_BACKEND": "bogus"}
    with pytest.raises(ConfigError, match="backend"):
        load_config(path=toml, env=env)


def test_env_backend_override_skips_old_backend_placeholder(tmp_path):
    """P2: an explicit VISION_BACKEND override must not expand the stale
    TOML backend placeholder (it may reference a removed variable)."""
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "${OLD_BACKEND}"\n'
        'api_key = "sk-old-provider"\n'
        'model = "qwen-vl"\n'
        'base_url = "https://old-vision.example.com/v1"\n'
    )
    env = {
        "VISION_BACKEND": "anthropic",
        "VISION_API_KEY": "sk-anthropic-new",
        "VISION_MODEL": "claude-sonnet-4-5",
    }
    cfg = load_config(path=toml, env=env)
    assert cfg.vision.backend == "anthropic"
    assert cfg.vision.base_url == "https://api.anthropic.com"
    assert cfg.vision.api_key == "sk-anthropic-new"
    assert cfg.vision.model == "claude-sonnet-4-5"


def test_no_backend_override_expands_toml_placeholder_as_switch(tmp_path):
    """Without an override, a ${ENV} TOML backend expands and still counts
    as a switch: the old TOML key/model are not inherited."""
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "sk-ds-1"\n'
        "[vision]\n"
        'backend = "${OLD_BACKEND}"\n'
        'api_key = "sk-old-provider"\n'
        'model = "qwen-vl"\n'
        'base_url = "https://old-vision.example.com/v1"\n'
    )
    env = {"OLD_BACKEND": "anthropic"}
    with pytest.raises(ConfigError, match="VISION_API_KEY"):
        load_config(path=toml, env=env)

def _minimal_env() -> dict:
    return {
        "DEEPSEEK_API_KEY": "sk-ds-1",
        "VISION_API_KEY": "sk-v-1",
        "VISION_BASE_URL": "https://vision.example.com/v1",
        "VISION_MODEL": "qwen-vl-max",
    }


def test_retries_env_invalid_raises_config_error():
    env = _minimal_env()
    env["RETRIES"] = "abc"
    with pytest.raises(ConfigError, match="retries 必须是整数"):
        load_config(env=env)


def test_retries_env_negative_raises_config_error():
    env = _minimal_env()
    env["RETRIES"] = "-1"
    with pytest.raises(ConfigError, match="不能为负数"):
        load_config(env=env)


def test_retries_toml_negative_raises_config_error(tmp_path):
    toml = tmp_path / "deepsee.toml"
    toml.write_text(
        "[deepseek]\n"
        'api_key = "${DS_KEY}"\n'
        "retries = -3\n"
        "[vision]\n"
        'backend = "openai_compatible"\n'
        'api_key = "${VS_KEY}"\n'
        'model = "qwen-vl-max"\n'
        'base_url = "https://vision.example.com/v1"\n'
    )
    env = {"DS_KEY": "sk-ds-1", "VS_KEY": "sk-v-1"}
    with pytest.raises(ConfigError, match="不能为负数"):
        load_config(path=toml, env=env)
