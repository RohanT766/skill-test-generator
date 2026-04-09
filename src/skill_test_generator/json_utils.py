"""Shared JSON extraction utilities for LLM output parsing."""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_json(text: str) -> dict:
    """Extract a JSON object from LLM output, handling markdown fences,
    JS-style comments, trailing commas, and truncated output."""
    text = text.strip().removeprefix("\ufeff")

    fence = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n\s*```", text)
    if fence:
        text = fence.group(1).strip()

    if "{" not in text:
        raise ValueError("No JSON object found in LLM output")

    brace_start = text.index("{")
    depth = 0
    in_string = False
    escape_next = False
    end = brace_start
    for i, ch in enumerate(text[brace_start:], brace_start):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == brace_start:
        last_brace = text.rfind("}")
        if last_brace > brace_start:
            end = last_brace

    raw = text[brace_start : end + 1]

    def _clean_js(s: str) -> str:
        s = re.sub(r"//[^\n]*", "", s)
        s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
        s = re.sub(r",\s*([}\]])", r"\1", s)
        return s

    for candidate in (raw, _clean_js(raw)):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    repaired = _clean_js(raw)
    quote_count = repaired.count('"') - repaired.count('\\"')
    if quote_count % 2 == 1:
        repaired += '"'
    open_braces = repaired.count("{") - repaired.count("}")
    open_brackets = repaired.count("[") - repaired.count("]")
    repaired = repaired.rstrip(", \n\t")
    repaired += "]" * max(0, open_brackets)
    repaired += "}" * max(0, open_braces)
    try:
        result = json.loads(repaired)
        logger.warning(
            "Repaired truncated JSON (closed %d braces, %d brackets)",
            max(0, open_braces),
            max(0, open_brackets),
        )
        return result
    except json.JSONDecodeError:
        pass

    raise ValueError(
        f"Failed to parse JSON from LLM output (first 500 chars): {raw[:500]}"
    )
