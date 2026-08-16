from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "dsh-credentials.py"


def _run(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )


def test_has_reports_status_without_disclosing_value(tmp_path: Path):
    credentials = tmp_path / ".credentials.yaml"
    credentials.write_text(
        "DEEPSEEK_API_KEY: deepseek-secret\nDEEPSEE_DSV_API_KEY: dsv-secret\n",
        encoding="utf-8",
    )

    result = _run("has", "--file", str(credentials), "--name", "DEEPSEE_DSV_API_KEY")

    assert result.returncode == 0
    assert result.stdout.strip() == "present"
    assert "dsv-secret" not in result.stdout + result.stderr


def test_has_missing_is_nonzero_and_silent_about_values(tmp_path: Path):
    credentials = tmp_path / ".credentials.yaml"
    credentials.write_text("DEEPSEEK_API_KEY: deepseek-secret\n", encoding="utf-8")

    result = _run("has", "--file", str(credentials), "--name", "DEEPSEE_DSV_API_KEY")

    assert result.returncode == 1
    assert result.stdout.strip() == "missing"
    assert "deepseek-secret" not in result.stdout + result.stderr


def test_set_reads_key_from_stdin_preserves_other_entries_and_sets_mode(tmp_path: Path):
    credentials = tmp_path / ".credentials.yaml"
    credentials.write_text(
        "# keep this comment\nDEEPSEEK_API_KEY: deepseek-secret\nOTHER: value\n",
        encoding="utf-8",
    )
    os.chmod(credentials, 0o644)

    result = _run(
        "set",
        "--file",
        str(credentials),
        "--name",
        "DEEPSEE_DSV_API_KEY",
        "--stdin",
        input_text="dsv:new-secret\n",
    )

    assert result.returncode == 0, result.stderr
    content = credentials.read_text(encoding="utf-8")
    assert "# keep this comment\n" in content
    assert "DEEPSEEK_API_KEY: deepseek-secret\n" in content
    assert "OTHER: value\n" in content
    assert "DEEPSEE_DSV_API_KEY: \"dsv:new-secret\"\n" in content
    assert stat_mode(credentials) == 0o600
    assert "dsv:new-secret" not in result.stdout + result.stderr


def test_set_replaces_existing_key_without_accepting_argv_secret(tmp_path: Path):
    credentials = tmp_path / ".credentials.yaml"
    credentials.write_text("DEEPSEE_DSV_API_KEY: old-value\n", encoding="utf-8")

    result = _run(
        "set",
        "--file",
        str(credentials),
        "--name",
        "DEEPSEE_DSV_API_KEY",
        "--stdin",
        input_text="new-value\n",
    )

    assert result.returncode == 0, result.stderr
    assert "old-value" not in credentials.read_text(encoding="utf-8")
    assert 'DEEPSEE_DSV_API_KEY: "new-value"' in credentials.read_text(encoding="utf-8")


def test_set_rejects_empty_stdin_without_changing_file(tmp_path: Path):
    credentials = tmp_path / ".credentials.yaml"
    original = "OTHER: value\n"
    credentials.write_text(original, encoding="utf-8")

    result = _run(
        "set",
        "--file",
        str(credentials),
        "--name",
        "DEEPSEE_DSV_API_KEY",
        "--stdin",
        input_text="\n",
    )

    assert result.returncode == 2
    assert credentials.read_text(encoding="utf-8") == original


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
