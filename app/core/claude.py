"""Claude API client for story generation."""

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
) -> tuple[str, str]:
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

    raw = message.content[0].text.strip()
    theme, story = _parse_theme_and_story(raw)
    if not theme and desireCategory in THEME_BY_CATEGORY:
        theme = THEME_BY_CATEGORY[desireCategory]
    story = _cap_to_chars(story, STORY_MAX_CHARS)
    return theme, story


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
) -> tuple[str, str]:
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
    story = message.content[0].text.strip()
    story = _cap_to_chars(story, STORY_MAX_CHARS)
    theme = f"{original_theme} (Deepening #{deepening_count})"
    return theme, story
