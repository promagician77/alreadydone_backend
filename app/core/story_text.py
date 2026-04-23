"""Helpers for validating and preparing story text for narration."""

from __future__ import annotations

import re


_SENTENCE_END_RE = re.compile(r'[.!?]["\')\]]*\s*$')
_TRAILING_CLAUSE_RE = re.compile(r"(and|but|so|or|because|when|while|then|that|which|who|with|as|if)$", re.IGNORECASE)
_BRACKETED_CUE_RE = re.compile(r"\[(.*?)\]")
_PAREN_CUE_RE = re.compile(r"\((.*?)\)")
_SFX_HINT_RE = re.compile(
    r"\b(sfx|sound effect|ambient|background noise|music|meow|woof|bark|buzz|ring|clang|beep|applause|laughs?|gasp|sigh)\b",
    re.IGNORECASE,
)


def trim_to_sentence_boundary(text: str, max_chars: int) -> tuple[str, bool]:
    """Trim text to a clean sentence boundary when possible."""
    cleaned = (text or "").strip()
    if len(cleaned) <= max_chars:
        return cleaned, False

    candidate = cleaned[:max_chars].rstrip()
    last_sentence_boundary = None
    for match in re.finditer(r'[.!?]["\')\]]*(?:\s|$)', candidate):
        boundary = match.end()
        if boundary >= int(max_chars * 0.65):
            last_sentence_boundary = boundary
    if last_sentence_boundary is not None:
        return candidate[:last_sentence_boundary].rstrip(), True

    for idx in range(len(candidate) - 1, max(int(max_chars * 0.65), 0), -1):
        if candidate[idx].isspace():
            return candidate[:idx].rstrip(), True
    return candidate.rstrip(), True


def story_ends_cleanly(text: str) -> bool:
    """Return True when the text appears to end on a full sentence."""
    cleaned = (text or "").strip()
    if not cleaned:
        return False
    if _SENTENCE_END_RE.search(cleaned):
        return True
    return False


def looks_like_clipped_ending(text: str) -> bool:
    """Best-effort heuristic for detecting stories that stop mid-thought."""
    cleaned = (text or "").strip()
    if not cleaned:
        return True
    if story_ends_cleanly(cleaned):
        return False
    last_word = cleaned.split()[-1].strip('"\')]}').lower() if cleaned.split() else ""
    if _TRAILING_CLAUSE_RE.search(last_word):
        return True
    if cleaned.endswith((",", ";", ":", "-", "(", "[", "{", "/")):
        return True
    return True


def ensure_complete_story_text(text: str, *, max_chars: int | None = None) -> tuple[str, dict]:
    """Trim safely and reject text that still appears incomplete."""
    cleaned = (text or "").strip()
    trimmed = False
    if max_chars is not None:
        cleaned, trimmed = trim_to_sentence_boundary(cleaned, max_chars)

    metadata = {
        "trimmed_to_sentence_boundary": trimmed,
        "looks_incomplete": looks_like_clipped_ending(cleaned),
        "ends_cleanly": story_ends_cleanly(cleaned),
        "final_chars": len(cleaned),
    }
    if metadata["looks_incomplete"]:
        raise ValueError("Generated story appears incomplete and was not persisted.")
    return cleaned, metadata


def _remove_bracketed_sound_cues(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        inner = match.group(1).strip()
        return " " if _SFX_HINT_RE.search(inner) else match.group(0)

    text = _BRACKETED_CUE_RE.sub(repl, text)
    text = _PAREN_CUE_RE.sub(repl, text)
    return text


def prepare_story_for_narration(text: str) -> str:
    """Remove obvious sound-effect cues before sending text to TTS."""
    cleaned = (text or "").strip()
    if not cleaned:
        return cleaned

    cleaned = _remove_bracketed_sound_cues(cleaned)
    cleaned = re.sub(r"\b(?:SFX|Sound effect)\s*:\s*[^\n]+", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()
