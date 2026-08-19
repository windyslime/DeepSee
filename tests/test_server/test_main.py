import sys
import pytest


from deepsee_server import __main__ as server_main
from deepsee_server.app import configure_request_guard
from deepsee_server.auth import configure_api_key_store


def test_cli_mounts_web_dist_before_starting_uvicorn(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        server_main,
        "server_settings",
        lambda: type("Settings", (), {"host": "127.0.0.1", "port": 8712})(),
    )
    monkeypatch.setattr(
        server_main,
        "mount_web_dist",
        lambda path: calls.append(("mount", path)) or tmp_path.resolve(),
    )
    monkeypatch.setattr(
        server_main.uvicorn,
        "run",
        lambda application, **kwargs: calls.append(("run", application, kwargs)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deepsee-server",
            "--web-dist",
            str(tmp_path),
            "--keys-file",
            str(tmp_path / "keys.json"),
        ],
    )

    try:
        server_main.main()
    finally:
        configure_api_key_store(None)
        configure_request_guard(None)

    assert calls[0] == ("mount", str(tmp_path))
    assert calls[1][0] == "run"
    assert calls[1][2] == {"host": "127.0.0.1", "port": 8712}


def test_cli_rejects_corrupted_key_file_before_starting(tmp_path, monkeypatch):
    """损坏的 key 文件在启动前被拒绝,进程以非零码退出。"""
    keys_file = tmp_path / "keys.json"
    keys_file.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        server_main,
        "server_settings",
        lambda: type("Settings", (), {"host": "127.0.0.1", "port": 8712})(),
    )
    monkeypatch.setattr(
        server_main,
        "uvicorn",
        type("Uvicorn", (), {"run": lambda **kwargs: None})(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["deepsee-server", "--keys-file", str(keys_file)],
    )

    try:
        with pytest.raises(SystemExit) as exc:
            server_main.main()
        assert exc.value.code == 2
    finally:
        configure_api_key_store(None)
        configure_request_guard(None)


def test_cli_explicitly_enables_browser_restart_control(tmp_path, monkeypatch):
    created = []
    configured = []
    monkeypatch.setattr(
        server_main,
        "server_settings",
        lambda: type("Settings", (), {"host": "127.0.0.1", "port": 8712})(),
    )
    monkeypatch.setattr(
        server_main,
        "RestartController",
        lambda *, enabled: created.append(enabled) or object(),
        raising=False,
    )
    monkeypatch.setattr(
        server_main,
        "configure_restart_controller",
        lambda controller: configured.append(controller),
        raising=False,
    )
    monkeypatch.setattr(server_main.uvicorn, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "deepsee-server",
            "--no-auth",
            "--allow-browser-restart",
            "--keys-file",
            str(tmp_path / "keys.json"),
        ],
    )

    server_main.main()

    assert created == [True]
    assert len(configured) == 1
