"""Structured output parsing for the vision pipeline."""

from __future__ import annotations

import json
import re
from typing import Any


def parse_structured(text: str | None) -> dict[str, Any] | None:
    """Extract the first balanced JSON object from model output.

    Tolerates ```json fences, surrounding prose, and stray characters.
    Returns ``None`` when no valid JSON object can be extracted.
    """
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = candidate.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(candidate)):
        ch = candidate[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(candidate[start : i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None
