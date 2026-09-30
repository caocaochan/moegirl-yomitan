from __future__ import annotations

import re


_WHITESPACE_RE = re.compile(r"\s+")
_SENTENCE_PUNCTUATION = "。！？!?；;"


def normalize_whitespace(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def trim_summary(text: str, limit: int) -> str:
    if type(limit) is not int or limit <= 0:
        raise ValueError("summary limit must be a positive integer")
    text = normalize_whitespace(text)
    if len(text) <= limit:
        return text

    cutoff = text[:limit]
    floor = max(0, int(limit * 0.55))

    for punctuation in _SENTENCE_PUNCTUATION:
        position = cutoff.rfind(punctuation)
        if position >= floor:
            return cutoff[: position + 1].rstrip()

    whitespace_position = cutoff.rfind(" ")
    if whitespace_position >= floor:
        return cutoff[:whitespace_position].rstrip() + "…"

    hard_limit = max(0, limit - 1)
    return cutoff[:hard_limit].rstrip() + "…"
