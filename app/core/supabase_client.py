"""Supabase client singleton."""

import logging

from supabase import create_client

from app.core.config import settings

_client = None


def get_supabase():
    global _client
    if _client is None:
        if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
            raise RuntimeError("SUPABASE_URL and SUPABASE_KEY must be set")
        url, key = settings.SUPABASE_URL, settings.SUPABASE_KEY
        try:
            from supabase.lib.client_options import ClientOptions

            options = ClientOptions(
                postgrest_client_timeout=settings.SUPABASE_POSTGREST_CLIENT_TIMEOUT_SECONDS,
                storage_client_timeout=settings.SUPABASE_STORAGE_CLIENT_TIMEOUT_SECONDS,
            )
            _client = create_client(url, key, options=options)
        except Exception as exc:
            logging.warning(
                "Supabase ClientOptions not applied (%s); using default httpx timeouts. "
                "Upgrade supabase-py if storage uploads hit ReadTimeout.",
                exc,
            )
            _client = create_client(url, key)
    return _client
