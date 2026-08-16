#!/usr/bin/env python3
"""Safely inspect and update a DSH credential mapping without exposing values."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path


NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
TOP_LEVEL_RE = re.compile(
    r"^(?P<prefix>[ \t]*)(?P<name>[A-Za-z_][A-Za-z0-9_-]*)(?P<separator>:[ \t]*)(?P<value>.*?)(?P<newline>\r?\n)?$"
)


class CredentialError(ValueError):
    """Raised when a credential file cannot be safely handled."""


def _validate_name(name: str) -> None:
    if not NAME_RE.fullmatch(name):
        raise CredentialError("credential name must be an uppercase environment name")


def _value_is_present(value: str) -> bool:
    value = value.strip()
    if not value or value.startswith("#"):
        return False
    return value not in {"''", '""', "null", "~"}


def _find_entry(lines: list[str], name: str) -> int | None:
    for index, line in enumerate(lines):
        match = TOP_LEVEL_RE.match(line)
        if match is None or match.group("prefix"):
            continue
        if match.group("name") == name:
            return index
    return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def has(path: Path, name: str) -> bool:
    _validate_name(name)
    text = _read_text(path)
    lines = text.splitlines(keepends=True)
    index = _find_entry(lines, name)
    if index is None:
        return False
    match = TOP_LEVEL_RE.match(lines[index])
    return match is not None and _value_is_present(match.group("value"))


def _atomic_write(path: Path, text: str) -> None:
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = temporary.name
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def set_from_stdin(path: Path, name: str) -> None:
    _validate_name(name)
    value = sys.stdin.read()
    if value.endswith("\n"):
        value = value[:-1]
        if value.endswith("\r"):
            value = value[:-1]
    if not value or "\n" in value or "\r" in value:
        raise CredentialError("stdin must contain one non-empty credential value")

    text = _read_text(path)
    lines = text.splitlines(keepends=True)
    index = _find_entry(lines, name)
    newline = "\n"
    if index is not None:
        old_line = lines[index]
        newline_match = TOP_LEVEL_RE.match(old_line)
        if newline_match and newline_match.group("newline"):
            newline = newline_match.group("newline")
    elif text and not text.endswith(("\n", "\r")):
        text += "\n"
        lines = text.splitlines(keepends=True)

    rendered = f"{name}: {json.dumps(value, ensure_ascii=False)}{newline}"
    if index is None:
        lines.append(rendered)
    else:
        lines[index] = rendered
    _atomic_write(path, "".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("has", "set"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--file", required=True, type=Path)
        command_parser.add_argument("--name", required=True)
        if command == "set":
            command_parser.add_argument("--stdin", action="store_true", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "has":
            present = has(args.file, args.name)
            print("present" if present else "missing")
            return 0 if present else 1
        set_from_stdin(args.file, args.name)
        print("updated")
        return 0
    except (CredentialError, OSError) as exc:
        print(f"credential operation failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
