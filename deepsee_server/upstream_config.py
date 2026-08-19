"""Server-managed upstream provider configuration.

The browser-facing configuration seam owns one private JSON document and
exposes typed values; callers never manipulate the on-disk shape directly.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from deepsee.config.loader import VISION_MODES, Config, load_config, load_config_candidate

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_VISION_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_VISION_MODEL = "qwen-vl-max"


def validate_provider_base_url(value: str, field: str) -> None:
    """Reject URLs that are incomplete or could expose embedded credentials."""

    try:
        parsed = urlsplit(value)
        parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} 必须是有效的 HTTP 或 HTTPS URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
        or any(character.isspace() for character in value)
    ):
        raise ValueError(f"{field} 必须是有效的 HTTP 或 HTTPS URL")


def default_upstream_config_path() -> Path:
    return Path.home() / ".config" / "deepsee" / "upstream.json"


@dataclass(frozen=True)
class ManagedProviderConfig:
    api_key: str | None
    base_url: str
    model: str
    api_key_inherited: bool = False
    models: dict[str, str] = field(default_factory=dict)

    def model_for_mode(self, mode: str) -> str:
        if mode not in VISION_MODES:
            raise ValueError(f"视觉模式必须为 {', '.join(VISION_MODES)}")
        return self.models.get(mode) or self.model


@dataclass(frozen=True)
class ManagedUpstreamConfig:
    deepseek: ManagedProviderConfig
    vision: ManagedProviderConfig
    vision_backend: str = "openai_compatible"


class UpstreamConfigStore:
    """Atomically persist and load one managed upstream configuration."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    @property
    def backup_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.bak")

    def load(self) -> ManagedUpstreamConfig | None:
        if not self.path.exists():
            return None
        with self.path.open(encoding="utf-8") as handle:
            raw = json.load(handle)
        return self._parse(raw)

    def save(self, config: ManagedUpstreamConfig) -> None:
        self._parse(self._payload(config))
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        previous: bytes | None = None
        if self.path.exists():
            self.load()
            previous = self.path.read_bytes()
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
                os.chmod(temporary_name, 0o600)
                json.dump(self._payload(config), temporary, ensure_ascii=True, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            if previous is not None:
                backup_name: str | None = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=self.path.parent,
                        prefix=f".{self.backup_path.name}.",
                        delete=False,
                    ) as backup:
                        backup_name = backup.name
                        os.chmod(backup_name, 0o600)
                        backup.write(previous)
                        backup.flush()
                        os.fsync(backup.fileno())
                    os.replace(backup_name, self.backup_path)
                finally:
                    if backup_name is not None and os.path.exists(backup_name):
                        os.unlink(backup_name)
            os.replace(temporary_name, self.path)
        finally:
            if temporary_name is not None and os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def _payload(config: ManagedUpstreamConfig) -> dict:
        deepseek = {
            "base_url": config.deepseek.base_url,
            "model": config.deepseek.model,
        }
        if not config.deepseek.api_key_inherited:
            deepseek["api_key"] = config.deepseek.api_key
        vision = {
            "backend": config.vision_backend,
            "base_url": config.vision.base_url,
            "model": config.vision.model,
        }
        if config.vision.models:
            vision["models"] = dict(config.vision.models)
        if not config.vision.api_key_inherited:
            vision["api_key"] = config.vision.api_key
        return {
            "version": 1,
            "deepseek": deepseek,
            "vision": vision,
        }

    @staticmethod
    def _parse(raw: object) -> ManagedUpstreamConfig:
        if not isinstance(raw, dict) or raw.get("version") != 1:
            raise ValueError("managed upstream config must be a version 1 object")
        if set(raw) != {"version", "deepseek", "vision"}:
            raise ValueError("managed upstream config has invalid fields")
        deepseek = raw.get("deepseek")
        vision = raw.get("vision")
        if not isinstance(deepseek, dict) or not isinstance(vision, dict):
            raise ValueError("managed upstream config requires provider objects")

        def provider(value: dict, name: str) -> ManagedProviderConfig:
            required = {"base_url", "model"}
            if name == "vision":
                required.add("backend")
            allowed = required | {"api_key", "models"}
            if not required.issubset(value) or not set(value).issubset(allowed):
                raise ValueError(f"{name} has invalid fields")
            api_key = value.get("api_key")
            base_url = value.get("base_url")
            model = value.get("model")
            if api_key is not None and (not isinstance(api_key, str) or not api_key):
                raise ValueError(f"{name}.api_key must be a non-empty string or null")
            if not isinstance(base_url, str) or not base_url:
                raise ValueError(f"{name}.base_url must be a non-empty string")
            validate_provider_base_url(base_url, f"{name}.base_url")
            if not isinstance(model, str) or not model:
                raise ValueError(f"{name}.model must be a non-empty string")
            raw_models = value.get("models", {})
            if not isinstance(raw_models, dict):
                raise ValueError(f"{name}.models must be an object")
            unknown_modes = set(raw_models) - set(VISION_MODES)
            if unknown_modes:
                raise ValueError(
                    f"{name}.models has invalid modes: "
                    + ", ".join(sorted(str(item) for item in unknown_modes))
                )
            models: dict[str, str] = {}
            for mode, mode_model in raw_models.items():
                if not isinstance(mode_model, str) or not mode_model.strip():
                    raise ValueError(
                        f"{name}.models.{mode} must be a non-empty string"
                    )
                models[mode] = mode_model.strip()
            return ManagedProviderConfig(
                api_key=api_key,
                base_url=base_url,
                model=model,
                api_key_inherited="api_key" not in value,
                models=models,
            )

        backend = vision.get("backend")
        if backend != "openai_compatible":
            raise ValueError("vision.backend must be openai_compatible")
        return ManagedUpstreamConfig(
            deepseek=provider(deepseek, "deepseek"),
            vision=provider(vision, "vision"),
            vision_backend=backend,
        )


def load_effective_config(
    store: UpstreamConfigStore,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Load environment > managed JSON > TOML through the existing validator."""

    return effective_config_from_managed(store.load(), env)


def effective_config_from_managed(
    managed: ManagedUpstreamConfig | None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Validate a managed candidate without persisting it."""

    effective_env = _effective_environment(managed, env)
    return load_config(env=effective_env)


def _effective_environment(
    managed: ManagedUpstreamConfig | None,
    env: Mapping[str, str] | None,
) -> dict[str, str]:
    effective_env = dict(os.environ) if env is None else dict(env)
    if managed is not None:
        values = (
            (
                "DEEPSEEK_API_KEY",
                managed.deepseek.api_key,
                managed.deepseek.api_key_inherited,
            ),
            ("DEEPSEEK_BASE_URL", managed.deepseek.base_url, False),
            ("DEEPSEEK_MODEL", managed.deepseek.model, False),
            ("VISION_BACKEND", managed.vision_backend, False),
            (
                "VISION_API_KEY",
                managed.vision.api_key,
                managed.vision.api_key_inherited,
            ),
            ("VISION_BASE_URL", managed.vision.base_url, False),
            ("VISION_MODEL", managed.vision.model, False),
        )
        for name, value, inherited in values:
            if name in effective_env or f"DeepSee_{name}" in effective_env:
                continue
            if inherited:
                continue
            effective_env[f"DeepSee_{name}"] = value or ""
        for mode in VISION_MODES:
            model = managed.vision.models.get(mode)
            if model is None:
                continue
            name = f"VISION_MODEL_{mode.upper()}"
            if name in effective_env or f"DeepSee_{name}" in effective_env:
                continue
            effective_env[f"DeepSee_{name}"] = model
    return effective_env


def resolve_effective_config(
    managed: ManagedUpstreamConfig | None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Resolve field precedence without requiring provider credentials."""

    return load_config_candidate(env=_effective_environment(managed, env))


def redacted_config_view(
    store: UpstreamConfigStore,
    env: Mapping[str, str] | None = None,
) -> dict:
    """Return effective non-secrets plus write-only credential state."""

    environment = dict(os.environ) if env is None else dict(env)
    managed = store.load()
    effective = resolve_effective_config(managed, environment)

    deepseek_base_url = (
        effective.deepseek.base_url or DEFAULT_DEEPSEEK_BASE_URL
    )
    deepseek_model = (
        effective.deepseek.model or DEFAULT_DEEPSEEK_MODEL
    )
    vision_base_url = (
        effective.vision.base_url or DEFAULT_VISION_BASE_URL
    )
    vision_model = (
        effective.vision.model or DEFAULT_VISION_MODEL
    )
    vision_backend = effective.vision.backend

    mode_models: dict[str, str] = {}
    mode_states: dict[str, dict[str, object]] = {}
    for mode in VISION_MODES:
        name = f"VISION_MODEL_{mode.upper()}"
        supplied = name in environment or f"DeepSee_{name}" in environment
        value = effective.vision.model_for_mode(mode) or vision_model
        state: dict[str, object] = {
            "model": value,
            "modelWritable": not supplied,
        }
        if supplied:
            state["modelSource"] = "env"
        elif managed is not None and mode in managed.vision.models:
            state["modelSource"] = "managed"
        elif effective.vision.model:
            state["modelSource"] = "toml"
        mode_models[mode] = value
        mode_states[mode] = state

    def key_state(
        name: str,
        managed_provider: ManagedProviderConfig | None,
        resolved_value: str,
    ) -> dict:
        prefixed = f"DeepSee_{name}"
        if prefixed in environment or name in environment:
            value = environment.get(prefixed, environment.get(name, ""))
            return {
                "keyConfigured": bool(value),
                "keySource": "env",
                "keyWritable": False,
            }
        if managed_provider is not None and not managed_provider.api_key_inherited:
            configured = bool(managed_provider.api_key)
            return {
                "keyConfigured": configured,
                **({"keySource": "managed"} if configured else {}),
                "keyWritable": True,
            }
        configured = bool(resolved_value)
        return {
            "keyConfigured": configured,
            **({"keySource": "toml"} if configured else {}),
            "keyWritable": True,
        }

    def field_state(field: str, environment_name: str) -> dict:
        environment_supplies = (
            environment_name in environment
            or f"DeepSee_{environment_name}" in environment
        )
        return {
            f"{field}Writable": not environment_supplies,
            **({f"{field}Source": "env"} if environment_supplies else {}),
        }

    return {
        "deepseek": {
            "baseUrl": deepseek_base_url,
            "model": deepseek_model,
            **field_state("baseUrl", "DEEPSEEK_BASE_URL"),
            **field_state("model", "DEEPSEEK_MODEL"),
            **key_state(
                "DEEPSEEK_API_KEY",
                managed.deepseek if managed is not None else None,
                effective.deepseek.api_key,
            ),
        },
        "vision": {
            "backend": vision_backend,
            "baseUrl": vision_base_url,
            "model": vision_model,
            "models": mode_models,
            "modelStates": mode_states,
            **field_state("backend", "VISION_BACKEND"),
            **field_state("baseUrl", "VISION_BASE_URL"),
            **field_state("model", "VISION_MODEL"),
            **key_state(
                "VISION_API_KEY",
                managed.vision if managed is not None else None,
                effective.vision.api_key,
            ),
        },
    }
