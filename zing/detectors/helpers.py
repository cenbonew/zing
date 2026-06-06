"""Shared building blocks for detectors.

Centralized so every detector generates filler text, needles, and parses model
output the same way — important when several detectors compare results.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from zing.utils.tokenize import estimate_tokens

# A pool of natural-ish sentences for filler. Varied so it is not trivially
# compressible by a relay's summarizer shim (which would defeat truncation tests).
_FILLER_SENTENCES = [
    "The quarterly logistics report noted unusual variance in regional throughput.",
    "Migratory patterns of arctic terns span nearly the entire globe each year.",
    "A well-tempered clavier requires careful attention to equal temperament tuning.",
    "The committee deferred the zoning amendment pending an environmental survey.",
    "Photosynthesis converts light energy into chemical energy stored in glucose.",
    "Renaissance cartographers often embellished unknown regions with sea monsters.",
    "The compiler emitted a warning about an unused variable in the hot loop.",
    "Tidal forces gradually slow the Earth's rotation over geological timescales.",
    "Her thesis examined supply-chain resilience under correlated demand shocks.",
    "The orchestra rehearsed the adagio movement until the phrasing felt natural.",
]


def stable_marker(seed: str, prefix: str = "ZING") -> str:
    """A reproducible high-entropy marker derived from ``seed``.

    Used as a needle/canary that a model cannot have memorized.
    """
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10].upper()
    return f"{prefix}-{digest}"


def build_filler(target_tokens: int, tokenizer: str | None = None) -> str:
    """Build filler text of approximately ``target_tokens`` tokens.

    Lines are numbered so position is meaningful and the text resists dedup.
    """
    if target_tokens <= 0:
        return ""
    parts: list[str] = []
    idx = 0
    # Grow in chunks, re-estimating periodically to avoid quadratic cost.
    while True:
        sentence = _FILLER_SENTENCES[idx % len(_FILLER_SENTENCES)]
        parts.append(f"[{idx:05d}] {sentence}")
        idx += 1
        if idx % 50 == 0 and estimate_tokens("\n".join(parts), tokenizer) >= target_tokens:
            break
        if idx > 400_000:  # hard safety stop
            break
    return "\n".join(parts)


def build_haystack(
    *,
    total_tokens: int,
    needle: str,
    depth: float,
    tokenizer: str | None = None,
    instruction: str = "",
) -> str:
    """Embed ``needle`` at fractional ``depth`` (0.0=start, 1.0=end) in filler.

    The returned text is a full prompt: filler with the needle sentence inserted,
    followed by a retrieval instruction.
    """
    depth = max(0.0, min(1.0, depth))
    filler = build_filler(total_tokens, tokenizer)
    lines = filler.split("\n")
    needle_line = (
        f"IMPORTANT FACT: The secret pass phrase is {needle}. "
        f"Remember it exactly."
    )
    insert_at = int(len(lines) * depth)
    lines.insert(insert_at, needle_line)
    body = "\n".join(lines)
    tail = instruction or (
        "\n\nNow answer this question using only the text above: "
        "What is the secret pass phrase? Reply with only the pass phrase."
    )
    return body + tail


def contains_ci(haystack: str, needle: str) -> bool:
    """Case-insensitive substring test, tolerant of surrounding whitespace."""
    return needle.strip().lower() in (haystack or "").lower()


def contains_all(text: str, needles: list[str]) -> bool:
    return all(contains_ci(text, n) for n in needles)


def contains_any(text: str, needles: list[str]) -> bool:
    return any(contains_ci(text, n) for n in needles)


def contains_word(text: str, word: str) -> bool:
    """Case-insensitive WHOLE-WORD match (boundaries on non-alphanumerics).

    Unlike :func:`contains_ci`, this won't match a brand inside a longer token —
    e.g. ``"meta"`` does not match ``"metadata"`` and ``"gpt"`` does not match
    ``"ChatGPT"`` — which matters for identity brand checks. Internal hyphens/dots
    in the term (``"gpt-5"``) are matched literally.
    """
    word = (word or "").strip()
    if not word:
        return False
    pattern = r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])"
    return re.search(pattern, text or "", re.IGNORECASE) is not None


def contains_word_any(text: str, words: list[str]) -> bool:
    return any(contains_word(text, w) for w in words)


def words_present(text: str, words: list[str]) -> list[str]:
    """Return the subset of ``words`` present in ``text`` as whole words."""
    return [w for w in (words or []) if contains_word(text, w)]


def first_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from text, tolerating code fences/prose."""
    if not text:
        return None
    text = text.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def usage_field(usage: dict[str, Any] | None, *names: str) -> int | None:
    """Read the first present integer field from a usage dict (handles aliases)."""
    if not isinstance(usage, dict):
        return None
    for name in names:
        value = usage.get(name)
        if isinstance(value, (int, float)):
            return int(value)
    return None
