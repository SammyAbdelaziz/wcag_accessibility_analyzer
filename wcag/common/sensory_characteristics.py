"""Shared WCAG 1.3.3 (Sensory Characteristics) detector.

Looks for instructions that depend solely on visual cues — color, shape, or
position — without naming the control. Used by HTML / DOCX / PPTX / XLSX
analyzers so the rule fires consistently across formats.
"""
from __future__ import annotations

import re
from typing import Iterable, List, Dict, Any


# Action verbs that introduce an instruction.
_ACTION = r"(?:click|press|tap|select|choose|use|see|look\s+at|find|locate)"
# UI nouns that the verb is acting on.
_NOUN = r"(?:button|icon|link|tab|box|item|element|circle|square|arrow|dot|marker|menu|panel)"


_COLOR_RE = re.compile(
    rf"\b{_ACTION}\b\s+(?:the\s+)?"
    r"(?:red|green|blue|yellow|orange|purple|pink|black|white|gray|grey|brown)\s+"
    rf"{_NOUN}\b",
    re.IGNORECASE,
)
_POSITION_RE = re.compile(
    rf"\b{_ACTION}\b\s+(?:the\s+)?"
    rf"{_NOUN}\s+(?:on|at|to|in)\s+the\s+"
    r"(?:right|left|top|bottom|upper|lower|center)\b",
    re.IGNORECASE,
)
_SHAPE_RE = re.compile(
    rf"\b{_ACTION}\b\s+(?:the\s+)?"
    r"(?:round|circular|square|rectangular|triangular|diamond-shaped|star-shaped)\s+"
    rf"{_NOUN}\b",
    re.IGNORECASE,
)


def find_sensory_phrases(texts: Iterable[Any]) -> List[Dict[str, Any]]:
    """Scan an iterable of (index, text) tuples for sensory-only references.

    Returns a list of dicts: {kind, index, snippet}. One hit per (index, kind).
    """
    results: List[Dict[str, Any]] = []
    for index, text in texts:
        if not text:
            continue
        for kind, pat in (("color", _COLOR_RE),
                          ("position", _POSITION_RE),
                          ("shape", _SHAPE_RE)):
            m = pat.search(text)
            if m:
                results.append({
                    "kind": kind,
                    "index": index,
                    "snippet": m.group(0)[:80],
                })
                break  # only one finding per text item
    return results
