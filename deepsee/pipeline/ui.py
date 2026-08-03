"""Structured output parsing for the vision pipeline."""

from __future__ import annotations

import json
import re
from typing import Any

_decoder = json.JSONDecoder()


def parse_structured(text: str | None) -> dict[str, Any] | None:
    """Extract the first JSON object from model output.

    Uses ``json.JSONDecoder.raw_decode`` so string contents (including
    braces and escapes) are handled correctly; tolerates ```json fences,
    surrounding prose, and stray characters. Returns ``None`` when no
    valid JSON object can be extracted.
    """
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start = 0
    while True:
        start = candidate.find("{", start)
        if start == -1:
            return None
        try:
            parsed, _ = _decoder.raw_decode(candidate[start:])
        except json.JSONDecodeError:
            start += 1
            continue
        return parsed if isinstance(parsed, dict) else None


def normalize_ui_map(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce a parsed UI analysis dict into a type-safe shape.

    Model output is untrusted: element lists may be non-lists, booleans
    may be strings, and text fields may be numbers. Normalize so that
    downstream formatting never raises on shape alone, and safety-relevant
    fields like ``target_found`` only take effect when actually boolean.
    """

    def _str(value: Any) -> str:
        return value if isinstance(value, str) else ""

    elements = data.get("elements")
    if not isinstance(elements, list):
        elements = []
    elements = [el for el in elements if isinstance(el, dict)]

    target_found = data.get("target_found")
    if not isinstance(target_found, bool):
        target_found = None

    return {
        "ui_type": _str(data.get("ui_type")),
        "layout": _str(data.get("layout")),
        "elements": elements,
        "target_found": target_found,
        "rescreenshot_advice": _str(data.get("rescreenshot_advice")),
        "answer_to_user": _str(data.get("answer_to_user")),
    }
