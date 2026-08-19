"""Configuration loading: TOML file + environment variables.

Priority: environment variables > TOML file > defaults.
"""

from __future__ import annotations

import os

try:  # Python >= 3.11
    import tomllib
except ModuleNotFoundError:  # Python 3.10: use the official backport
    import tomli as tomllib  # type: ignore[no-redef]

from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from deepsee.errors import ConfigError

VALID_BACKENDS = ("openai_compatible", "anthropic", "gemini")
VISION_MODES = ("auto", "ui", "general")
_VISION_MODE_ENV_KEYS = {
    mode: f"VISION_MODEL_{mode.upper()}" for mode in VISION_MODES
}

DEFAULT_VISION_BASE_URLS: dict[str, str | None] = {
    "openai_compatible": None,  # user must provide
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
}

ENV_PREFIX = "DeepSee_"


@dataclass
class DeepSeekConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"


@dataclass
class VisionConfig:
    backend: str
    api_key: str
    model: str
    base_url: str | None = None
    models: dict[str, str] = field(default_factory=dict)

    def model_for_mode(self, mode: str) -> str:
        if mode not in VISION_MODES:
            raise ValueError(f"视觉模式必须为 {', '.join(VISION_MODES)}")
        return self.models.get(mode) or self.model


@dataclass
class Config:
    deepseek: DeepSeekConfig
    vision: VisionConfig
    retries: int = 2


def _resolve_file(path: str | os.PathLike | None) -> Path | None:
    if path is not None:
        return Path(path)
    cwd = Path.cwd() / "deepsee.toml"
    if cwd.is_file():
        return cwd
    home = Path.home() / ".config" / "deepsee" / "deepsee.toml"
    if home.is_file():
        return home
    return None


def _read_toml(file: Path) -> dict:
    try:
        with open(file, "rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置文件 {file}: TOML 格式无效或文件不可读") from exc


def _expand_env(value: str, env: Mapping[str, str]) -> str:
    if value.startswith("${") and value.endswith("}"):
        name = value[2:-1]
        if name not in env:
            raise ConfigError(
                f"配置引用环境变量 {name} 但未设置;请设置环境变量或直接写入配置值"
            )
        return env[name]
    return value


def _validate_base_url(value: str, section: str) -> None:
    try:
        parsed = urlsplit(value)
        parsed.port
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigError(f"{section}.base_url 必须是有效的 HTTP 或 HTTPS URL") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
        or any(character.isspace() for character in value)
    ):
        raise ConfigError(f"{section}.base_url 必须是有效的 HTTP 或 HTTPS URL")


def _validate(
    deepseek: DeepSeekConfig,
    vision: VisionConfig,
) -> None:
    if not deepseek.api_key:
        raise ConfigError("缺少 deepseek.api_key:请配置 DeepSeek API key")
    if not vision.api_key:
        raise ConfigError("缺少 vision.api_key:请配置视觉模型 API key")
    if vision.backend not in VALID_BACKENDS:
        raise ConfigError(
            f"vision.backend 非法: {vision.backend!r};"
            f"可选值: {', '.join(VALID_BACKENDS)}"
        )
    _validate_base_url(deepseek.base_url, "deepseek")
    if vision.base_url is None:
        raise ConfigError("缺少 vision.base_url:当前后端必须显式提供 base_url")
    _validate_base_url(vision.base_url, "vision")
    if not vision.model:
        raise ConfigError("缺少 vision.model:当前后端必须显式提供模型")
    for mode in VISION_MODES:
        if not vision.model_for_mode(mode):
            raise ConfigError(f"缺少 vision.models.{mode}:当前后端必须显式提供模型")


def _load_config(
    path: str | os.PathLike | None = None,
    env: Mapping[str, str] | None = None,
    *,
    validate: bool,
) -> Config:
    """Load config from TOML file (optional) merged with environment variables.

    Search order for the file: explicit ``path``, ``./deepsee.toml``,
    ``~/.config/deepsee/deepsee.toml``. Missing file is fine — environment
    variables alone can configure DeepSee.
    """
    env = dict(os.environ) if env is None else dict(env)
    file = _resolve_file(path)
    raw: dict = {}
    if file is not None:
        raw = _read_toml(file)
    for section in ("deepseek", "vision"):
        value = raw.get(section, {})
        if not isinstance(value, dict):
            raise ConfigError(f"{section} 必须是 TOML 表")

    # TOML values, with ${ENV} expansion
    def toml_str(section: str, key: str, default: str = "") -> str:
        value = raw.get(section, {}).get(key, default)
        if value == "":
            return default
        return _expand_env(str(value), env)

    # Raw TOML value without ${ENV} expansion: used to detect whether the
    # TOML backend is itself an environment reference.
    def toml_raw_str(section: str, key: str, default: str = "") -> str:
        value = raw.get(section, {}).get(key, default)
        if value == "":
            return default
        return str(value)

    retries_toml = raw.get("deepseek", {}).get("retries", 2)
    try:
        retries = int(retries_toml)
    except (TypeError, ValueError):
        raise ConfigError(f"retries 必须是整数,当前: {retries_toml!r}")
    if retries < 0:
        raise ConfigError(f"retries 不能为负数,当前: {retries}")

    vision_backend_raw = toml_raw_str("vision", "backend", "openai_compatible")

    # Environment overrides (highest priority).
    # Accept both the prefixed form (DeepSee_DEEPSEEK_API_KEY) and the bare
    # form (DEEPSEEK_API_KEY); prefixed wins.
    def env_val(section_key: str, toml_value: str) -> str:
        prefixed = env.get(f"{ENV_PREFIX}{section_key}")
        if prefixed is not None:
            return prefixed
        return env.get(section_key, toml_value)

    def env_has(section_key: str) -> bool:
        return f"{ENV_PREFIX}{section_key}" in env or section_key in env

    def toml_mode_str(mode: str, default: str = "") -> str:
        models = raw.get("vision", {}).get("models", {})
        if models is None:
            models = {}
        if not isinstance(models, dict):
            raise ConfigError("vision.models 必须是 TOML 表")
        unknown = set(models) - set(VISION_MODES)
        if unknown:
            raise ConfigError(
                "vision.models 包含未知模式: "
                + ", ".join(sorted(str(value) for value in unknown))
            )
        value = models.get(mode, default)
        if value == "":
            return default
        return _expand_env(str(value), env)

    def env_mode_val(mode: str, toml_value: str) -> str:
        aliases = (_VISION_MODE_ENV_KEYS[mode], f"VISION_{mode.upper()}_MODEL")
        for alias in aliases:
            prefixed = env.get(f"{ENV_PREFIX}{alias}")
            if prefixed is not None:
                return prefixed
            if alias in env:
                return env[alias]
        return toml_value

    def env_mode_has(mode: str) -> bool:
        aliases = (_VISION_MODE_ENV_KEYS[mode], f"VISION_{mode.upper()}_MODEL")
        return any(
            f"{ENV_PREFIX}{alias}" in env or alias in env for alias in aliases
        )

    # backend + base_url + api_key + model 是一个配置束。backend 被切换时,
    # 旧 TOML 的三项一律不得继承:
    # - base_url:仍指向旧 backend 的主机,沿用会把新 backend 的 API key 发送
    #   到错误主机;切换时丢弃,回落到新 backend 的官方默认主机(用户仍可用
    #   VISION_BASE_URL 显式覆盖)。
    # - api_key / model:属于旧供应商,不得继承;新 backend 必须由环境变量
    #   显式提供,否则抛 ConfigError,而不是静默把旧供应商密钥发给新供应商。
    # 切换判定:
    # - TOML backend 为字面量且与环境 VISION_BACKEND 同值 → 未切换,TOML
    #   配置原样保留(自定义代理/审计/数据驻留场景依赖这一行为);
    # - TOML backend 为 ${ENV} 插值 → 一律按切换处理(展开后与 env_val 的
    #   结果必然同值,按值比较会误判"未切换"而保留旧 base_url);
    # - 其余值异变 → 切换。
    # 解析顺序:环境显式覆盖 VISION_BACKEND 时,TOML backend 完全无需展开
    # —— 它可能是引用已删除变量的 ${ENV} 占位符,提前展开会在新配置已完整
    # 时仍报错;仅在没有环境覆盖时才展开 TOML backend(其引用的环境变量缺失
    # 时报错是合理的)。
    backend_env_value = env.get(f"{ENV_PREFIX}VISION_BACKEND")
    if backend_env_value is None:
        backend_env_value = env.get("VISION_BACKEND")
    if backend_env_value is not None:
        vision_backend = backend_env_value
        vision_backend_toml = vision_backend_raw
    else:
        vision_backend_toml = toml_str("vision", "backend", "openai_compatible")
        vision_backend = vision_backend_toml
    if vision_backend not in VALID_BACKENDS:
        raise ConfigError(
            f"vision.backend 非法: {vision_backend!r};"
            f"可选值: {', '.join(VALID_BACKENDS)}"
        )
    backend_env_driven = vision_backend_raw.startswith("${") and vision_backend_raw.endswith("}")
    backend_switched = backend_env_driven or vision_backend != vision_backend_toml
    vision_base_url_toml = "" if backend_switched else toml_str("vision", "base_url", "")
    vision_base_url = vision_base_url_toml or DEFAULT_VISION_BASE_URLS.get(vision_backend) or ""
    mode_env_complete = all(env_mode_has(mode) for mode in VISION_MODES)
    legacy_model_env = env_has("VISION_MODEL")
    if backend_switched:
        if validate and not env_has("VISION_API_KEY"):
            raise ConfigError(
                f"VISION_BACKEND 已将后端切换为 {vision_backend!r},但未显式提供"
                "新的 VISION_API_KEY;切换后端后不能继承旧后端的 API key,"
                "请设置 DeepSee_VISION_API_KEY 或 VISION_API_KEY"
            )
        if validate and not (legacy_model_env or mode_env_complete):
            raise ConfigError(
                f"VISION_BACKEND 已将后端切换为 {vision_backend!r},但未显式提供"
                "新的 VISION_MODEL;切换后端后不能继承旧后端的模型,"
                "请设置 DeepSee_VISION_MODEL 或 VISION_MODEL"
            )
        # 切换分支完全不读取旧 TOML 的 key/model —— 其 ${ENV} 占位符可能
        # 引用了已失效的环境变量,提前展开会在新配置已完整时仍报错。
        vision_api_key = env_val("VISION_API_KEY", "")
        vision_model = env_val("VISION_MODEL", "")
        mode_models = {
            mode: env_mode_val(mode, "")
            for mode in VISION_MODES
            if env_mode_val(mode, "")
        }
    else:
        vision_api_key = env_val("VISION_API_KEY", toml_str("vision", "api_key"))
        vision_model = env_val("VISION_MODEL", toml_str("vision", "model"))
        mode_models = {
            mode: env_mode_val(mode, toml_mode_str(mode))
            for mode in VISION_MODES
            if env_mode_val(mode, toml_mode_str(mode))
        }

    if not vision_model:
        vision_model = (
            mode_models.get("auto")
            or mode_models.get("general")
            or mode_models.get("ui")
            or ""
        )

    deepseek = DeepSeekConfig(
        api_key=env_val("DEEPSEEK_API_KEY", toml_str("deepseek", "api_key")),
        base_url=env_val("DEEPSEEK_BASE_URL", toml_str("deepseek", "base_url", "https://api.deepseek.com")),
        model=env_val("DEEPSEEK_MODEL", toml_str("deepseek", "model", "deepseek-chat")),
    )
    vision = VisionConfig(
        backend=vision_backend,
        api_key=vision_api_key,
        model=vision_model,
        base_url=env_val("VISION_BASE_URL", vision_base_url) or None,
        models=mode_models,
    )
    retries_raw = env_val("RETRIES", str(retries))
    try:
        retries = int(retries_raw)
    except (TypeError, ValueError):
        raise ConfigError(f"retries 必须是整数,当前: {retries_raw!r}")
    if retries < 0:
        raise ConfigError(f"retries 不能为负数,当前: {retries}")

    if validate:
        _validate(deepseek, vision)
    return Config(deepseek=deepseek, vision=vision, retries=retries)


def load_config(
    path: str | os.PathLike | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Load and fully validate the effective runtime configuration."""

    return _load_config(path, env, validate=True)


def load_config_candidate(
    path: str | os.PathLike | None = None,
    env: Mapping[str, str] | None = None,
) -> Config:
    """Resolve precedence without requiring both provider credentials."""

    return _load_config(path, env, validate=False)
