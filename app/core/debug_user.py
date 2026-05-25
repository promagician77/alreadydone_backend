"""Conditional debug prints for a specific user account."""

from __future__ import annotations

import logging
from typing import Any

DEBUG_USER_EMAILS = (
    "b6d8bnzjck@privaterelay.appleid.com",
    "madridmaestro01@gmail.com",
)

_user_id_cache: dict[int, bool] = {}
_email_by_user_id_cache: dict[int, str | None] = {}


def _normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


_DEBUG_EMAILS_NORMALIZED = frozenset(_normalize_email(e) for e in DEBUG_USER_EMAILS)


def is_debug_email(email: str | None) -> bool:
    return _normalize_email(email) in _DEBUG_EMAILS_NORMALIZED


def is_debug_user_id(supabase, user_id: int | str | None) -> bool:
    if user_id is None:
        return False
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return False
    if uid in _user_id_cache:
        return _user_id_cache[uid]
    match = False
    resolved_email: str | None = None
    try:
        r = supabase.table("Users").select("email").eq("id", uid).limit(1).execute()
        rows = list(r.data or [])
        resolved_email = rows[0].get("email") if rows else None
        match = is_debug_email(resolved_email)
    except Exception:
        logging.exception("[debug_user] failed to resolve email for user_id=%s", uid)
    _user_id_cache[uid] = match
    _email_by_user_id_cache[uid] = _normalize_email(resolved_email) if resolved_email else None
    return match


def _resolve_debug_email(
    *,
    user_id: int | str | None,
    email: str | None,
    supabase,
) -> str | None:
    if email is not None and is_debug_email(email):
        return _normalize_email(email)
    if user_id is None or supabase is None:
        return None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    if uid not in _email_by_user_id_cache and not is_debug_user_id(supabase, uid):
        return None
    return _email_by_user_id_cache.get(uid)


def user_id_from_story(supabase, story_id: int) -> int | None:
    try:
        r = (
            supabase.table("Stories")
            .select("user_id")
            .eq("id", story_id)
            .limit(1)
            .execute()
        )
        rows = list(r.data or [])
        if not rows:
            return None
        raw = rows[0].get("user_id") or rows[0].get("userId")
        return int(raw) if raw is not None else None
    except Exception:
        logging.exception("[debug_user] failed to resolve user_id for story_id=%s", story_id)
        return None


def _should_log(*, user_id: int | str | None, email: str | None, supabase) -> bool:
    if email is not None and is_debug_email(email):
        return True
    if user_id is not None and supabase is not None:
        return is_debug_user_id(supabase, user_id)
    return False


def debug_log(
    location: str,
    message: str,
    *,
    user_id: int | str | None = None,
    email: str | None = None,
    supabase=None,
    **data: Any,
) -> None:
    """Print and log only for debug users (by user_id or email)."""
    if not _should_log(user_id=user_id, email=email, supabase=supabase):
        return
    matched = _resolve_debug_email(user_id=user_id, email=email, supabase=supabase) or "debug_user"
    parts = [f"[DEBUG {matched}]", location, message]
    if user_id is not None:
        parts.append(f"user_id={user_id}")
    if data:
        parts.append(f"data={data}")
    line = " | ".join(parts)
    print(line, flush=True)
    logging.info(line)
