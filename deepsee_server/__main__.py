"""Run the DeepSee server: ``python -m deepsee_server``.

Host/port come from environment or deepsee.toml ``[server]`` (default
127.0.0.1:8712); command-line flags override them for one-off runs.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from deepsee_server.app import app, configure_request_guard, mount_web_dist
from deepsee_server.auth import (
    ApiKeyStore,
    ApiKeyStoreError,
    configure_api_key_store,
    disable_api_key_auth,
)
from deepsee_server.config import request_guard_settings, server_settings
from deepsee_server.request_guard import RequestGuard
from deepsee_server.runtime_control import RestartController, configure_restart_controller


def validate_no_auth_host(host: str) -> None:
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("--no-auth 只允许 loopback host")


def _create_key_pair(store: ApiKeyStore, *, recovery: bool) -> None:
    public = store.create(
        "public", "recovery-public" if recovery else "default-public"
    )
    admin = store.create(
        "admin", "recovery-admin" if recovery else "default-admin"
    )
    print(f"Public API key: {public.key}")
    print(f"Admin API key: {admin.key}")


def main() -> None:
    settings = server_settings()
    parser = argparse.ArgumentParser(description="DeepSee OpenAI-compatible server")
    parser.add_argument("--host", default=settings.host, help=f"监听地址(默认 {settings.host})")
    parser.add_argument("--port", type=int, default=settings.port, help=f"监听端口(默认 {settings.port})")
    parser.add_argument(
        "--web-dist",
        default=None,
        help="可选的 Vite dist 目录;启用后由 FastAPI 同源托管网站",
    )
    parser.add_argument(
        "--keys-file",
        default=str(Path.home() / ".config" / "deepsee" / "api-keys.json"),
    )
    parser.add_argument("--no-auth", action="store_true")
    parser.add_argument("--create-recovery-keys", action="store_true")
    parser.add_argument(
        "--allow-browser-restart",
        action="store_true",
        help="允许受 launchd 管理的进程由 Admin API 请求重启",
    )
    args = parser.parse_args()

    if args.no_auth:
        validate_no_auth_host(args.host)

    guard_settings = request_guard_settings()
    configure_request_guard(
        RequestGuard(
            max_concurrent=guard_settings.max_concurrent,
            queue_timeout=guard_settings.queue_timeout,
            rate_limit=guard_settings.rate_limit,
            rate_window=guard_settings.rate_window,
        )
    )

    if args.no_auth:
        disable_api_key_auth()
    else:
        store = ApiKeyStore(args.keys_file)
        try:
            # 启动时验证 key-store schema:文件损坏立即失败,而不是带病运行
            store.check()
        except ApiKeyStoreError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            sys.exit(2)
        if store.is_empty():
            _create_key_pair(store, recovery=False)
        if args.create_recovery_keys:
            _create_key_pair(store, recovery=True)
        configure_api_key_store(store)
    configure_restart_controller(
        RestartController(enabled=args.allow_browser_restart)
    )
    if args.web_dist:
        mounted = mount_web_dist(args.web_dist)
        print(f"DeepSee website: http://{args.host}:{args.port}/ ({mounted})")
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
