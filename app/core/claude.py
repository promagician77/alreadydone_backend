"""Claude API client for story generation."""

from __future__ import annotations

import logging
import re
from anthropic import AsyncAnthropic

from app.core.config import (
    settings,
    STORY_MAX_CHARS,
    THEME_BY_CATEGORY,
    OUTPUT_FORMAT_INSTRUCTION,
)
from app.core.story_prompts import CLIENT_SYSTEM_PROMPT, get_story_user_prompt
from app.core.deepen_prompts import DEEPEN_SYSTEM_PROMPT, get_deepen_user_prompt
from app.core.story_text import ensure_complete_story_text, looks_like_clipped_ending


def _user_data(
    *,
    name: str,
    location: str,
    energyWord: str,
    desireCategory: str,
    desireDescription: str,
    lovedOne: str | None,
    storyCount: int,
    previousStoryThemes: list[str],
) -> dict:
    return {
        "name": name,
        "location": location,
        "energyWord": energyWord,
        "lovedOne": lovedOne or "Not provided",
        "desireCategory": desireCategory,
        "desireDescription": desireDescription,
        "storyCount": storyCount,
        "previousStoryThemes": previousStoryThemes,
    }


def _parse_theme_and_story(raw: str) -> tuple[str, str]:
    """Parse 'THEME: ...' or 'TITLE: ...' from first line; rest is story."""
    raw = raw.strip()
    theme_match = re.match(r"^(?:THEME|TITLE):\s*(.+?)(?:\n|$)", raw, re.IGNORECASE | re.DOTALL)
    if theme_match:
        theme = theme_match.group(1).strip()
        story = raw[theme_match.end() :].strip().lstrip("\n").strip()
    else:
        theme = ""
        story = raw
    return theme, story


def _cap_to_chars(text: str, max_chars: int) -> str:
    """Return text truncated to at most max_chars (by character)."""
    if len(text) <= max_chars:
        return text
    return text[: max_chars].rstrip()


def _usage_as_dict(message) -> dict:
    usage = getattr(message, "usage", None)
    if usage is None:
        return {}
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
    }


def _extract_text_from_message(message) -> str:
    if not getattr(message, "content", None):
        return ""
    parts: list[str] = []
    for block in message.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "".join(parts).strip()


async def _request_continuation(
    *,
    client: AsyncAnthropic,
    system_prompt: str,
    prior_text: str,
    continuation_label: str,
) -> tuple[str, dict]:
    continuation_prompt = (
        f"The {continuation_label} below was cut off before it finished.\n\n"
        "Continue from the exact next words.\n"
        "Do not repeat prior text.\n"
        "Do not add headers, theme lines, titles, notes, or explanations.\n"
        "Finish with a complete ending.\n\n"
        f"{continuation_label.upper()} SO FAR:\n{prior_text}"
    )
    message = await client.messages.create(
        model=settings.CLAUDE_STORY_MODEL,
        max_tokens=settings.CLAUDE_STORY_CONTINUATION_MAX_TOKENS,
        system=system_prompt,
        messages=[{"role": "user", "content": continuation_prompt}],
    )
    return _extract_text_from_message(message), {
        "stop_reason": getattr(message, "stop_reason", None),
        "usage": _usage_as_dict(message),
    }


async def _finalize_story_output(
    *,
    client: AsyncAnthropic,
    system_prompt: str,
    initial_text: str,
    continuation_label: str,
    max_chars: int,
) -> tuple[str, dict]:
    raw_text = (initial_text or "").strip()
    metadata = {
        "initial_raw_chars": len(raw_text),
        "continuations_used": 0,
        "continuation_stop_reasons": [],
    }

    for _ in range(max(0, settings.CLAUDE_STORY_MAX_CONTINUATIONS)):
        if raw_text and not looks_like_clipped_ending(raw_text):
            break
        continuation, continuation_meta = await _request_continuation(
            client=client,
            system_prompt=system_prompt,
            prior_text=raw_text,
            continuation_label=continuation_label,
        )
        continuation = continuation.strip()
        if not continuation:
            break
        raw_text = f"{raw_text.rstrip()} {continuation.lstrip()}".strip()
        metadata["continuations_used"] += 1
        metadata["continuation_stop_reasons"].append(continuation_meta.get("stop_reason"))
        metadata.setdefault("continuation_usage", []).append(continuation_meta.get("usage", {}))

    final_text, completion_meta = ensure_complete_story_text(raw_text, max_chars=max_chars)
    metadata.update(completion_meta)
    metadata["raw_chars_before_trim"] = len(raw_text)
    return final_text, metadata


# Theme extraction prompt (from client extractStoryTheme) for story evolution tracking
EXTRACT_THEME_USER = """Read this manifestation story and extract the main theme in 2-4 words:

{story}

Theme:"""


async def extract_story_theme(story_text: str) -> str:
    """Extract the main theme in 2-4 words from a story (for previous_story_themes / evolution)."""
    if not story_text or not story_text.strip():
        return ""
    if not settings.ANTHROPIC_API_KEY:
        return ""
    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        message = await client.messages.create(
            model=settings.CLAUDE_STORY_MODEL,
            max_tokens=50,
            messages=[{"role": "user", "content": EXTRACT_THEME_USER.format(story=story_text.strip())}],
        )
    except Exception as e:
        logging.warning("Theme extraction failed: %s", e)
        return ""
    if not message.content or not message.content[0].text:
        return ""
    return message.content[0].text.strip()


async def generate_story(
    name: str,
    location: str,
    energyWord: str,
    desireCategory: str,
    desireDescription: str,
    lovedOne: str | None = None,
    storyCount: int = 1,
    previousStoryThemes: list[str] | None = None,
    system_prompt: str | None = None,
) -> tuple[str, str, dict]:
    """Generate a past-tense personal story. Returns (theme, story). Story is capped at STORY_MAX_CHARS."""
    if not settings.ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY is not set")
    base_prompt = (system_prompt or settings.STORY_SYSTEM_PROMPT or "").strip() or CLIENT_SYSTEM_PROMPT.strip()
    prompt = base_prompt.rstrip() + OUTPUT_FORMAT_INSTRUCTION
    themes = previousStoryThemes or []
    user_data = _user_data(
        name=name,
        location=location,
        energyWord=energyWord,
        desireCategory=desireCategory,
        desireDescription=desireDescription,
        lovedOne=lovedOne,
        storyCount=storyCount,
        previousStoryThemes=themes,
    )
    user_message = get_story_user_prompt(storyCount, user_data)

    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        message = await client.messages.create(
            model=settings.CLAUDE_STORY_MODEL,
            max_tokens=settings.CLAUDE_STORY_MAX_TOKENS,
            system=prompt,
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as e:
        logging.exception("Claude API error: %s", e)
        raise

    if not message.content or not message.content[0].text:
        raise ValueError("Claude returned no text")

    raw = _extract_text_from_message(message)
    theme, story = _parse_theme_and_story(raw)
    if not theme and desireCategory in THEME_BY_CATEGORY:
        theme = THEME_BY_CATEGORY[desireCategory]
    story, completion_meta = await _finalize_story_output(
        client=client,
        system_prompt=prompt,
        initial_text=story,
        continuation_label="story",
        max_chars=STORY_MAX_CHARS,
    )
    metadata = {
        "model": settings.CLAUDE_STORY_MODEL,
        "max_tokens": settings.CLAUDE_STORY_MAX_TOKENS,
        "stop_reason": getattr(message, "stop_reason", None),
        "usage": _usage_as_dict(message),
        "raw_chars": len(raw),
        "theme": theme,
    }
    metadata.update(completion_meta)
    return theme, story, metadata


async def generate_deepen_story(
    *,
    user_name: str,
    location: str,
    energy_word: str,
    loved_one_name: str,
    original_desire_category: str,
    original_theme: str,
    previous_story_text: str,
    deepening_count: int,
) -> tuple[str, str, dict]:
    """Generate a deepening continuation story. Returns (theme, story). Story is capped at STORY_MAX_CHARS."""
    if not settings.ANTHROPIC_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY is not set")
    user_message = get_deepen_user_prompt(
        user_name=user_name,
        location=location,
        energy_word=energy_word,
        loved_one_name=loved_one_name or "Not provided",
        original_desire_category=original_desire_category,
        previous_story_text=previous_story_text.strip() or "(No previous story)",
        deepening_count=deepening_count,
    )
    client = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        message = await client.messages.create(
            model=settings.CLAUDE_STORY_MODEL,
            max_tokens=settings.CLAUDE_STORY_MAX_TOKENS,
            system=DEEPEN_SYSTEM_PROMPT.strip(),
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as e:
        logging.exception("Claude API error (deepen): %s", e)
        raise
    if not message.content or not message.content[0].text:
        raise ValueError("Claude returned no text")
    initial_story = _extract_text_from_message(message)
    story, completion_meta = await _finalize_story_output(
        client=client,
        system_prompt=DEEPEN_SYSTEM_PROMPT.strip(),
        initial_text=initial_story,
        continuation_label="story continuation",
        max_chars=STORY_MAX_CHARS,
    )
    theme = f"{original_theme} (Deepening #{deepening_count})"
    metadata = {
        "model": settings.CLAUDE_STORY_MODEL,
        "max_tokens": settings.CLAUDE_STORY_MAX_TOKENS,
        "stop_reason": getattr(message, "stop_reason", None),
        "usage": _usage_as_dict(message),
        "raw_chars": len(initial_story),
        "theme": theme,
    }
    metadata.update(completion_meta)
    return theme, story, metadata
