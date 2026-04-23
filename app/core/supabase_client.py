"""Supabase client singleton."""

from supabase import create_client

from app.core.config import settings

_client = None


def get_supabase():
    global _client
    if _client is None:
        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
        _client = create_client(settings.SUPABASE_URL, settings.SUPABASE_KEY)
    return _client
