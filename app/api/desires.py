"""Desires table: list all desires (id, desireCategory)."""

from fastapi import APIRouter

from app.core.supabase_client import get_supabase

router = APIRouter(prefix="/desires", tags=["desires"])


@router.get("")
async def get_desires():
    """Get all rows from Desires table (id, desireCategory)."""
    supabase = get_supabase()
    r = supabase.table("Desires").select("id, desireCategory").execute()
    rows = list(r.data or [])
    return {"desires": rows}
