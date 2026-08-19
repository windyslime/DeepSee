import stat
import json
import pytest

from deepsee.errors import ConfigError
from deepsee_server.upstream_config import (
    ManagedProviderConfig,
    ManagedUpstreamConfig,
    UpstreamConfigStore,
    load_effective_config,
    redacted_config_view,
)


def test_managed_config_round_trips_with_owner_only_permissions(tmp_path):
    store = UpstreamConfigStore(tmp_path / "private" / "upstream.json")
    config = ManagedUpstreamConfig(
        deepseek=ManagedProviderConfig(
            api_key="deepseek-secret",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
        ),
        vision=ManagedProviderConfig(
            api_key="vision-secret",
            base_url="https://vision.example/v1",
            model="vision-model",
        ),
    )

    store.save(config)

    assert store.load() == config
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600


def test_effective_config_precedence_is_environment_then_managed_then_toml(
    tmp_path, monkeypatch
):
    (tmp_path / "deepsee.toml").write_text(
        """
[deepseek]
api_key = "toml-deepseek"
base_url = "https://toml-deepseek.example/v1"
model = "toml-deepseek-model"

[vision]
backend = "openai_compatible"
api_key = "toml-vision"
base_url = "https://toml-vision.example/v1"
model = "toml-vision-model"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    store = UpstreamConfigStore(tmp_path / "managed.json")
    store.save(
        ManagedUpstreamConfig(
            deepseek=ManagedProviderConfig(
                api_key="managed-deepseek",
                base_url="https://managed-deepseek.example/v1",
                model="managed-deepseek-model",
            ),
            vision=ManagedProviderConfig(
                api_key="managed-vision",
                base_url="https://managed-vision.example/v1",
                model="managed-vision-model",
            ),
        )
    )

    managed = load_effective_config(store, env={})
    overridden = load_effective_config(
        store,
        env={
            "DeepSee_DEEPSEEK_API_KEY": "env-deepseek",
            "DeepSee_DEEPSEEK_MODEL": "env-deepseek-model",
            "DeepSee_VISION_API_KEY": "env-vision",
            "DeepSee_VISION_MODEL": "env-vision-model",
        },
    )

    assert managed.deepseek.api_key == "managed-deepseek"
    assert managed.deepseek.base_url == "https://managed-deepseek.example/v1"
    assert managed.vision.api_key == "managed-vision"
    assert overridden.deepseek.api_key == "env-deepseek"
    assert overridden.deepseek.model == "env-deepseek-model"
    assert overridden.vision.api_key == "env-vision"
    assert overridden.vision.model == "env-vision-model"


def test_redacted_view_reports_key_state_without_returning_secret_values(tmp_path):
    store = UpstreamConfigStore(tmp_path / "upstream.json")
    store.save(
        ManagedUpstreamConfig(
            deepseek=ManagedProviderConfig(
                api_key="managed-deepseek-secret",
                base_url="https://api.deepseek.com",
                model="deepseek-chat",
            ),
            vision=ManagedProviderConfig(
                api_key="managed-vision-secret",
                base_url="https://vision.example/v1",
                model="vision-model",
            ),
        )
    )

    view = redacted_config_view(
        store,
        env={"DeepSee_DEEPSEEK_API_KEY": "environment-secret"},
    )
    serialized = json.dumps(view)

    assert view["deepseek"]["keyConfigured"] is True
    assert view["deepseek"]["keySource"] == "env"
    assert view["deepseek"]["keyWritable"] is False
    assert view["vision"]["keySource"] == "managed"
    assert view["vision"]["keyWritable"] is True
    assert "environment-secret" not in serialized
    assert "managed-deepseek-secret" not in serialized
    assert "managed-vision-secret" not in serialized


def test_removed_managed_key_masks_a_toml_credential(tmp_path, monkeypatch):
    (tmp_path / "deepsee.toml").write_text(
        """
[deepseek]
api_key = "toml-deepseek"

[vision]
backend = "openai_compatible"
api_key = "toml-vision"
base_url = "https://toml-vision.example/v1"
model = "toml-vision-model"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    store = UpstreamConfigStore(tmp_path / "upstream.json")
    store.save(
        ManagedUpstreamConfig(
            deepseek=ManagedProviderConfig(
                api_key=None,
                base_url="https://api.deepseek.com",
                model="deepseek-chat",
            ),
            vision=ManagedProviderConfig(
                api_key="managed-vision",
                base_url="https://managed-vision.example/v1",
                model="managed-vision-model",
            ),
        )
    )

    with pytest.raises(ConfigError, match="deepseek.api_key"):
        load_effective_config(store, env={})

    view = redacted_config_view(store, env={})
    assert view["deepseek"]["keyConfigured"] is False
    assert "keySource" not in view["deepseek"]


def test_inherited_key_round_trips_without_storing_a_secret(tmp_path):
    store = UpstreamConfigStore(tmp_path / "upstream.json")
    inherited = ManagedUpstreamConfig(
        deepseek=ManagedProviderConfig(
            api_key=None,
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            api_key_inherited=True,
        ),
        vision=ManagedProviderConfig(
            api_key="vision-secret",
            base_url="https://vision.example/v1",
            model="vision-model",
        ),
    )

    store.save(inherited)

    assert store.load() == inherited
    assert "api_key" not in json.loads(store.path.read_text())["deepseek"]


def test_redacted_view_resolves_each_provider_when_configuration_is_incomplete(
    tmp_path, monkeypatch
):
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
    store = UpstreamConfigStore(tmp_path / "upstream.json")

    view = redacted_config_view(
        store,
        env={"DeepSee_DEEPSEEK_BASE_URL": "https://env-deepseek.example/v1"},
    )

    assert view["deepseek"]["baseUrl"] == "https://env-deepseek.example/v1"
    assert view["deepseek"]["baseUrlSource"] == "env"
    assert view["deepseek"]["baseUrlWritable"] is False
    assert view["deepseek"]["keyConfigured"] is True
    assert view["deepseek"]["keySource"] == "toml"
    assert view["vision"]["keyConfigured"] is False


def test_replacing_managed_config_keeps_previous_valid_config_as_private_backup(
    tmp_path,
):
    store = UpstreamConfigStore(tmp_path / "upstream.json")
    first = ManagedUpstreamConfig(
        deepseek=ManagedProviderConfig("deepseek-one", "https://ds.one/v1", "ds-one"),
        vision=ManagedProviderConfig("vision-one", "https://vision.one/v1", "vision-one"),
    )
    second = ManagedUpstreamConfig(
        deepseek=ManagedProviderConfig("deepseek-two", "https://ds.two/v1", "ds-two"),
        vision=ManagedProviderConfig("vision-two", "https://vision.two/v1", "vision-two"),
    )

    store.save(first)
    store.save(second)

    assert UpstreamConfigStore(store.backup_path).load() == first
    assert store.load() == second
    assert stat.S_IMODE(store.backup_path.stat().st_mode) == 0o600


def test_invalid_managed_candidate_is_rejected_before_creating_a_file(tmp_path):
    store = UpstreamConfigStore(tmp_path / "upstream.json")
    invalid = ManagedUpstreamConfig(
        deepseek=ManagedProviderConfig("deepseek", "not-a-url", "deepseek-chat"),
        vision=ManagedProviderConfig("vision", "https://vision.example/v1", "vision"),
    )

    with pytest.raises(ValueError, match="HTTP"):
        store.save(invalid)

    assert store.path.exists() is False


@pytest.mark.parametrize(
    "base_url",
    ["https://", "https://user:secret@example.com/v1", "https://example.com/v1?key=secret"],
)
def test_managed_config_rejects_unsafe_or_incomplete_base_urls(tmp_path, base_url):
    store = UpstreamConfigStore(tmp_path / "upstream.json")
    invalid = ManagedUpstreamConfig(
        deepseek=ManagedProviderConfig("deepseek", base_url, "deepseek-chat"),
        vision=ManagedProviderConfig("vision", "https://vision.example/v1", "vision"),
    )

    with pytest.raises(ValueError, match="URL"):
        store.save(invalid)

    assert store.path.exists() is False
