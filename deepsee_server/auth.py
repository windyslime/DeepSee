"""Local API key storage and FastAPI authorization helpers."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import Request

KeyScope = Literal["public", "admin"]
AuthMode = Literal["unconfigured", "disabled", "enabled"]


@dataclass
class CreatedApiKey:
    id: str
    key: str
    scope: KeyScope
    label: str
    created_at: int


class ApiKeyStoreError(Exception):
    """API key 存储不可读或结构损坏。

    携带文件位置供日志使用,绝不携带任何 key 内容;上层应映射为
    503 ``configuration_error``(fail closed),而不是裸 500。
    """

    def __init__(self, path: Path, detail: str):
        super().__init__(f"API key store 不可读: {path} ({detail})")
        self.path = path
        self.detail = detail


class ApiKeyStore:
    """Persist SHA-256 API key digests; plaintext is returned only at creation."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            os.chmod(self.path, 0o600)

    def check(self) -> None:
        """启动时验证 key-store 文件:损坏立即抛 ``ApiKeyStoreError``(fail fast)。"""
        self._load()

    def _load(self) -> list[dict]:
        with self._lock:
            if not self.path.exists():
                return []
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApiKeyStoreError(self.path, "文件损坏或不可读") from exc
            if not isinstance(data, list):
                raise ApiKeyStoreError(self.path, "顶层必须是数组")
            for index, record in enumerate(data):
                if not isinstance(record, dict):
                    raise ApiKeyStoreError(self.path, f"第 {index} 条记录必须是对象")
                missing = [
                    field
                    for field in (
                        "id",
                        "digest",
                        "scope",
                        "label",
                        "created_at",
                        "revoked",
                    )
                    if field not in record
                ]
                if missing:
                    raise ApiKeyStoreError(
                        self.path, f"第 {index} 条记录缺少字段: {', '.join(missing)}"
                    )
                if not isinstance(record["digest"], str) or not isinstance(
                    record["scope"], str
                ):
                    raise ApiKeyStoreError(
                        self.path, f"第 {index} 条记录字段类型非法"
                    )
            return data

    def _save(self, records: list[dict]) -> None:
        payload = json.dumps(records, ensure_ascii=True, indent=2)
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
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, self.path)
            os.chmod(self.path, 0o600)
        finally:
            if temporary_name is not None and os.path.exists(temporary_name):
                os.unlink(temporary_name)

    def create(self, scope: KeyScope, label: str) -> CreatedApiKey:
        if scope not in ("public", "admin"):
            raise ValueError("scope must be public or admin")
        record_id = uuid.uuid4().hex
        token = f"ds_{scope}_{secrets.token_urlsafe(32)}"
        created_at = int(time.time())
        with self._lock:
            records = self._load()
            records.append(
                {
                    "id": record_id,
                    "digest": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    "scope": scope,
                    "label": label,
                    "created_at": created_at,
                    "revoked": False,
                }
            )
            self._save(records)
        return CreatedApiKey(record_id, token, scope, label, created_at)

    def is_empty(self) -> bool:
        return not self._load()

    def list(self) -> list[dict]:
        return [
            {key: value for key, value in record.items() if key != "digest"}
            for record in self._load()
        ]

    def validate(self, token: str, scope: KeyScope) -> bool:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return any(
            not record.get("revoked")
            and record.get("scope") == scope
            and hmac.compare_digest(str(record.get("digest", "")), digest)
            for record in self._load()
        )

    def revoke(self, record_id: str) -> bool:
        with self._lock:
            records = self._load()
            found = False
            for record in records:
                if record.get("id") == record_id:
                    record["revoked"] = True
                    found = True
            if found:
                self._save(records)
            return found


_api_key_store: ApiKeyStore | None = None
_auth_mode: AuthMode = "unconfigured"


def configure_api_key_store(store: ApiKeyStore | None) -> None:
    """Enable authentication with ``store`` or reset to fail-closed state."""
    global _api_key_store, _auth_mode
    _api_key_store = store
    _auth_mode = "enabled" if store is not None else "unconfigured"


def disable_api_key_auth() -> None:
    """Explicitly disable authentication for the CLI ``--no-auth`` mode."""
    global _api_key_store, _auth_mode
    _api_key_store = None
    _auth_mode = "disabled"


def api_key_auth_mode() -> AuthMode:
    return _auth_mode


def configured_api_key_store() -> ApiKeyStore | None:
    return _api_key_store


def public_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else ""


def admin_token(request: Request) -> str:
    return request.headers.get("x-deepsee-admin-key", "")


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
