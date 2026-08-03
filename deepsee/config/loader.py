"""Configuration loading: TOML file + environment variables.

Priority: environment variables > TOML file > defaults.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from deepsee.errors import ConfigError

VALID_BACKENDS = ("openai_compatible", "anthropic", "gemini")

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
    with open(file, "rb") as fh:
        return tomllib.load(fh)


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
    if not (value.startswith("http://") or value.startswith("https://")):
        raise ConfigError(f"{section}.base_url 必须是 http(s):// 开头的 URL,当前: {value}")


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


def load_config(
    path: str | os.PathLike | None = None,
    env: Mapping[str, str] | None = None,
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

    vision_backend_raw = toml_raw_str("vision", "backend", "openai_compatible")
    vision_backend_toml = toml_str("vision", "backend", "openai_compatible")

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

    # backend + base_url + api_key + model 是一个配置束。backend 被环境变量
    # 异值切换时,旧 TOML 的三项一律不得继承:
    # - base_url:仍指向旧 backend 的主机,沿用会把新 backend 的 API key 发送
    #   到错误主机;切换时丢弃,回落到新 backend 的官方默认主机(用户仍可用
    #   VISION_BASE_URL 显式覆盖)。
    # - api_key / model:属于旧供应商,不得继承;新 backend 必须由环境变量
    #   显式提供,否则抛 ConfigError,而不是静默把旧供应商密钥发给新供应商。
    # 环境变量与 TOML 同值(如 VISION_BACKEND=anthropic 且 TOML 也是
    # anthropic)不算切换,TOML 配置原样保留 —— 自定义代理/审计/数据驻留
    # 场景依赖这一行为。
    vision_backend = env_val("VISION_BACKEND", vision_backend_toml)
    if vision_backend not in VALID_BACKENDS:
        raise ConfigError(
            f"vision.backend 非法: {vision_backend!r};"
            f"可选值: {', '.join(VALID_BACKENDS)}"
        )
    # TOML 若用 ${ENV} 引用 backend(如 backend = "${VISION_BACKEND}"),
    # 展开后与 env_val 的结果必然同值,按值比较会误判为"未切换"而保留
    # 旧 base_url —— 环境插值得到的 backend 一律按切换处理。
    backend_env_driven = vision_backend_raw.startswith("${") and vision_backend_raw.endswith("}")
    backend_switched = backend_env_driven or vision_backend != vision_backend_toml
    vision_base_url_toml = "" if backend_switched else toml_str("vision", "base_url", "")
    vision_base_url = vision_base_url_toml or DEFAULT_VISION_BASE_URLS.get(vision_backend) or ""
    if backend_switched:
        if not env_has("VISION_API_KEY"):
            raise ConfigError(
                f"VISION_BACKEND 已将后端切换为 {vision_backend!r},但未显式提供"
                "新的 VISION_API_KEY;切换后端后不能继承旧后端的 API key,"
                "请设置 DeepSee_VISION_API_KEY 或 VISION_API_KEY"
            )
        if not env_has("VISION_MODEL"):
            raise ConfigError(
                f"VISION_BACKEND 已将后端切换为 {vision_backend!r},但未显式提供"
                "新的 VISION_MODEL;切换后端后不能继承旧后端的模型,"
                "请设置 DeepSee_VISION_MODEL 或 VISION_MODEL"
            )
        # 切换分支完全不读取旧 TOML 的 key/model —— 其 ${ENV} 占位符可能
        # 引用了已失效的环境变量,提前展开会在新配置已完整时仍报错。
        vision_api_key = env_val("VISION_API_KEY", "")
        vision_model = env_val("VISION_MODEL", "")
    else:
        vision_api_key = env_val("VISION_API_KEY", toml_str("vision", "api_key"))
        vision_model = env_val("VISION_MODEL", toml_str("vision", "model"))

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
    )
    retries = int(env_val("RETRIES", str(retries)))

    _validate(deepseek, vision)
    return Config(deepseek=deepseek, vision=vision, retries=retries)