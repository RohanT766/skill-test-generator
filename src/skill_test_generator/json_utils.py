"""Shared JSON extraction utilities for LLM output parsing."""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# Characters that must be escaped inside JSON strings (RFC 8259 §7)
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_CONTROL_MAP = {
    "\x00": "\\u0000", "\x01": "\\u0001", "\x02": "\\u0002",
    "\x03": "\\u0003", "\x04": "\\u0004", "\x05": "\\u0005",
    "\x06": "\\u0006", "\x07": "\\u0007", "\x08": "\\u0008",
    "\x0b": "\\u000b", "\x0c": "\\u000c", "\x0e": "\\u000e",
    "\x0f": "\\u000f", "\x10": "\\u0010", "\x11": "\\u0011",
    "\x12": "\\u0012", "\x13": "\\u0013", "\x14": "\\u0014",
    "\x15": "\\u0015", "\x16": "\\u0016", "\x17": "\\u0017",
    "\x18": "\\u0018", "\x19": "\\u0019", "\x1a": "\\u001a",
    "\x1b": "\\u001b", "\x1c": "\\u001c", "\x1d": "\\u001d",
    "\x1e": "\\u001e", "\x1f": "\\u001f",
}


def _fix_json_strings(text: str) -> str:
    """Walk through JSON text and fix unescaped characters inside strings.

    LLMs often emit literal newlines/tabs inside JSON string values instead of
    \\n / \\t.  This function re-escapes them so ``json.loads`` can succeed.
    """
    out: list[str] = []
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]
        if ch != '"':
            out.append(ch)
            i += 1
            continue

        # Start of a JSON string — collect everything until the closing quote
        out.append('"')
        i += 1
        while i < n:
            ch = text[i]
            if ch == '\\':
                # Escape sequence — keep the backslash + next char verbatim
                if i + 1 < n:
                    out.append(ch)
                    out.append(text[i + 1])
                    i += 2
                else:
                    out.append(ch)
                    i += 1
                continue
            if ch == '"':
                out.append('"')
                i += 1
                break
            # Literal newline/tab/CR inside a string — the LLM forgot to escape
            if ch == '\n':
                out.append('\\n')
                i += 1
                continue
            if ch == '\r':
                out.append('\\r')
                i += 1
                continue
            if ch == '\t':
                out.append('\\t')
                i += 1
                continue
            # Other control characters
            replacement = _CONTROL_MAP.get(ch)
            if replacement:
                out.append(replacement)
                i += 1
                continue
            out.append(ch)
            i += 1

    return "".join(out)


def _extract_outermost_object(text: str) -> str:
    """Find the outermost ``{…}`` in *text* using a state machine that
    tolerates literal newlines / tabs / control chars inside JSON strings
    (which LLMs emit constantly when the values are source code).
    """
    start = text.index("{")
    depth = 0
    in_string = False
    escape_next = False
    end = start

    for i in range(start, len(text)):
        ch = text[i]
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
            # Stay inside the string regardless of newlines / control chars.
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break

    if end == start:
        last_brace = text.rfind("}")
        if last_brace > start:
            end = last_brace

    return text[start : end + 1]


def _clean_js(s: str) -> str:
    s = re.sub(r"//[^\n]*", "", s)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r",\s*([}\]])", r"\1", s)
    return s


def extract_json(text: str) -> dict:
    """Extract a JSON object from LLM output, handling markdown fences,
    JS-style comments, trailing commas, unescaped string content, and
    truncated output."""
    text = text.strip().removeprefix("\ufeff")

    fence = re.search(r"```(?:json)?\s*\n([\s\S]*?)\n\s*```", text)
    if fence:
        text = fence.group(1).strip()

    if "{" not in text:
        raise ValueError("No JSON object found in LLM output")

    # Fix unescaped chars FIRST so brace-matching tracks in_string correctly,
    # then also try on the raw text as a fallback.
    fixed_text = _fix_json_strings(text)

    for source in (fixed_text, text):
        try:
            raw = _extract_outermost_object(source)
        except ValueError:
            continue

        candidates = [
            raw,
            _clean_js(raw),
        ]
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue

        # Truncation repair
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
            continue

    # Final fallback: grab everything between first { and last }
    first = text.index("{")
    last = text.rfind("}")
    if last > first:
        blob = text[first : last + 1]
        for attempt in (_fix_json_strings(blob), _clean_js(_fix_json_strings(blob))):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue

    raise ValueError(
        f"Failed to parse JSON from LLM output (first 500 chars): "
        f"{text[text.index('{'):text.index('{') + 500]}"
    )
