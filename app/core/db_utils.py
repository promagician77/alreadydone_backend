"""Helpers for resilient Supabase updates."""

from __future__ import annotations

import logging

from app.core.supabase_client import get_supabase


def safe_partial_update(
    *,
    table_name: str,
    id_field: str,
    record_id: int | str,
    base_payload: dict | None = None,
    optional_payload: dict | None = None,
) -> dict:
    """
    Apply required updates first, then best-effort optional fields one-by-one.

    Optional fields are skipped if the backing table does not have the column yet.
    """
    supabase = get_supabase()
    result = {"applied_optional_fields": [], "skipped_optional_fields": {}}

    if base_payload:
        supabase.table(table_name).update(base_payload).eq(id_field, record_id).execute()

    for key, value in (optional_payload or {}).items():
        try:
            supabase.table(table_name).update({key: value}).eq(id_field, record_id).execute()
            result["applied_optional_fields"].append(key)
        except Exception as exc:
            logging.warning(
                "Skipping optional %s.%s update for %s=%s: %s",
                table_name,
                key,
                id_field,
                record_id,
                exc,
            )
            result["skipped_optional_fields"][key] = str(exc)
    return result
