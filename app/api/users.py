import logging
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, HTTPException, Header

from pydantic import BaseModel, Field

from app.api.subscription import _fetch_subscription_from_stripe
from app.core.config import settings
from app.core.supabase_client import get_supabase

router = APIRouter(prefix="/users", tags=["users"])


def _parse_date(value) -> date | None:
    """Parse ISO string or timestamp to UTC date. Return None if missing/invalid."""
    if value is None:
        return None
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif isinstance(value, (int, float)):
            dt = datetime.fromtimestamp(value, tz=timezone.utc)
        else:
            return None
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.date()
    except (TypeError, ValueError):
        return None


def _get_streak_days(supabase, user_id: int) -> int:
    """
    Return current streak: consecutive calendar days (UTC) with at least one story played.
    Uses Stories.last_played; excludes soft-deleted stories.
    """
    r = supabase.table("Stories").select("last_played", "is_deleted").eq("user_id", user_id).execute()
    rows = r.data or []
    active_dates: set[date] = set()
    for row in rows:
        d = _parse_date(row.get("last_played") or row.get("lastPlayed"))
        if d is not None:
            active_dates.add(d)
    if not active_dates:
        return 0
    today = datetime.now(timezone.utc).date()
    # Start from today if active, else yesterday (streak can include "yesterday" if not played today yet)
    start = today if today in active_dates else (today - timedelta(days=1)) if (today - timedelta(days=1)) in active_dates else None
    if start is None:
        return 0
    streak = 0
    d = start
    while d in active_dates:
        streak += 1
        d -= timedelta(days=1)
    return streak


def _days_since(date_value) -> int:
    """Return days between date_value and now (UTC). If date_value is missing or invalid, return 0."""
    if date_value is None:
        return 0
    try:
        if isinstance(date_value, str):
            dt = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
        else:
            dt = date_value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0, delta.days)
    except (TypeError, ValueError):
        return 0


@router.get("/{user_id}")
async def get_user_info(user_id: int):
    supabase = get_supabase()
    r = supabase.table("Users").select("*").eq("id", user_id).execute()
    rows = list(r.data or [])
    if not rows:
        raise HTTPException(status_code=404, detail="User not found")
    user = dict(rows[0])
    subscription_id = user.get("stripe_subscription_id") or user.get("stripe_subscription_Id")

    if subscription_id:
        synced = _fetch_subscription_from_stripe(str(subscription_id))
        if synced:
            user["subscription_status"] = synced["status"]
            user["subscription_plan"] = synced["plan"]
            user["trial_end"] = synced["trial_end"]
            try:
                supabase.table("Users").update({
                    "subscription_status": synced["status"],
                    "subscription_plan": synced["plan"],
                    "trial_end": synced["trial_end"],
                }).eq("id", user_id).execute()
            except Exception as e:
                logging.exception("Failed to sync subscription in get_user_info: %s", e)

    sr = supabase.table("Stories").select("id, is_deleted").eq("user_id", user_id).execute()
    stories = sr.data or []
    story_count = len(stories)
    active = len(stories)
    user["story_count"] = story_count
    user["complete"] = story_count
    user["day_streak"] = _get_streak_days(supabase, user_id)
    user["active"] = active
    return user


class UserUpdateRequest(BaseModel):
    """Body for updating user settings. Only provided fields are updated."""

    speed: str | None = Field(None, description="Speed (text)")
    morningTime_Reminder: datetime | None = Field(None, description="Morning reminder time (ISO datetime)")
    bedTime_Reminder: datetime | None = Field(None, description="Bedtime reminder time (ISO datetime)")
    is_MorningTime_Reminder: bool | None = Field(None, description="Morning reminder on/off")
    is_BedTime_Reminder: bool | None = Field(None, description="Bedtime reminder on/off")
    name: str | None = Field(None, description="User's name")
    email: str | None = Field(None, description="User's email")
    password: str | None = Field(None, description="User's password")
    location: str | None = Field(None, description="User's location")
    energyWord: str | None = Field(None, description="User's energy word")
    lovedOne: str | None = Field(None, description="User's loved one")
    sleepTime: int | None = Field(None, description="User's sleep time (minutes)")
    fcm_token: str | None = Field(None, description="FCM device token for push notifications")
    timezone: str | None = Field(None, description="IANA timezone (e.g. America/Los_Angeles) for reminder times")
    # RevenueCat / alternate subscription provider
    rc_customer_id: str | None = Field(None, description="RevenueCat customer ID")
    rc_subscription_status: str | None = Field(None, description="RevenueCat subscription status")
    rc_subscription_plan: str | None = Field(None, description="RevenueCat subscription plan")
    subscription_provider: str | None = Field(None, description="Subscription provider (e.g. stripe, revenuecat)")

@router.patch("/{user_id}")
async def update_user(user_id: str, body: UserUpdateRequest):
    """
    Update Users table: Speed, Morning_Reminder, Bedtime_Reminder.
    Only fields present in the body are updated.
    """
    payload = {}
    if body.speed is not None:
        payload["speed"] = body.speed
    if body.morningTime_Reminder is not None:
        # Supabase column is timestamp (no tz); send naive "YYYY-MM-DD HH:MM:SS"
        payload["morningTime_Reminder"] = body.morningTime_Reminder.strftime("%Y-%m-%d %H:%M:%S")
    if body.bedTime_Reminder is not None:
        payload["bedTime_Reminder"] = body.bedTime_Reminder.strftime("%Y-%m-%d %H:%M:%S")
    if body.is_MorningTime_Reminder is not None:
        payload["is_MorningTime_Reminder"] = body.is_MorningTime_Reminder
    if body.is_BedTime_Reminder is not None:
        payload["is_BedTime_Reminder"] = body.is_BedTime_Reminder
    if body.sleepTime is not None:
        payload["sleepTime"] = body.sleepTime
    if body.name is not None:
        payload["name"] = body.name
    if body.email is not None:
        payload["email"] = body.email
    if body.password is not None:
        payload["password"] = body.password
    if body.location is not None:
        payload["location"] = body.location
    if body.energyWord is not None:
        payload["energyWord"] = body.energyWord
    if body.lovedOne is not None:
        payload["lovedOne"] = body.lovedOne
    if body.fcm_token is not None:
        payload["fcm_token"] = body.fcm_token
    if body.timezone is not None:
        payload["timezone"] = body.timezone
    if body.rc_customer_id is not None:
        payload["rc_customer_id"] = body.rc_customer_id
    if body.rc_subscription_status is not None:
        payload["rc_subscription_status"] = body.rc_subscription_status
    if body.rc_subscription_plan is not None:
        payload["rc_subscription_plan"] = body.rc_subscription_plan
    if body.subscription_provider is not None:
        payload["subscription_provider"] = body.subscription_provider

    print(payload)

    if not payload:
        raise HTTPException(status_code=400, detail="Provide at least one field to update")

    supabase = get_supabase()
    try:
        r = supabase.table("Users").update(payload).eq("id", user_id).execute()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Supabase error: {e}")

    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        uid = None
    day_streak = _get_streak_days(supabase, uid) if uid is not None else 0
    return {"updated": True, "data": r.data, "day_streak": day_streak}


async def _get_supabase_user_from_token(access_token: str) -> dict | None:
    """Return Supabase auth user from an access token, or None if invalid."""
    if not settings.SUPABASE_URL:
        return None
    token = (access_token or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        return None
    url = f"{settings.SUPABASE_URL.rstrip('/')}/auth/v1/user"
    headers = {
        "Authorization": f"Bearer {token}",
        "apikey": settings.SUPABASE_KEY,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code >= 400:
            return None
        data = resp.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


async def _delete_supabase_auth_user(auth_user_id: str) -> None:
    """Delete Supabase auth user via admin API (best-effort)."""
    if not settings.SUPABASE_URL or not settings.SUPABASE_KEY:
        return
    uid = (auth_user_id or "").strip()
    if not uid:
        return
    url = f"{settings.SUPABASE_URL.rstrip('/')}/auth/v1/admin/users/{uid}"
    headers = {
        "Authorization": f"Bearer {settings.SUPABASE_KEY}",
        "apikey": settings.SUPABASE_KEY,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        await client.delete(url, headers=headers)


@router.post("/{user_id}/close-account")
async def close_account(
    user_id: int,
    authorization: str | None = Header(None, alias="Authorization"),
):
    """
    Permanently delete the user's app data:
    - Delete Stories rows for the user
    - Remove associated audio files from Supabase Storage (Stories.storage)
    - Delete Users row
    - Delete Supabase Auth user (admin) (best-effort)

    Requires a valid Supabase access token in Authorization header.
    No JSON body required (avoids 422 when clients POST with an empty body).
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization")

    auth_user = await _get_supabase_user_from_token(authorization)
    if not auth_user:
        raise HTTPException(status_code=401, detail="Invalid Authorization")

    token_uid = (auth_user.get("id") or "").strip()
    token_email = (auth_user.get("email") or "").strip().lower()
    if not token_uid or not token_email:
        raise HTTPException(status_code=401, detail="Invalid user token")

    supabase = get_supabase()

    # Verify Users row belongs to this auth user (by email).
    ur = supabase.table("Users").select("id,email").eq("id", user_id).execute()
    urows = list(ur.data or [])
    if not urows:
        raise HTTPException(status_code=404, detail="User not found")
    user_row = urows[0]
    db_email = (user_row.get("email") or "").strip().lower()
    if db_email != token_email:
        raise HTTPException(status_code=403, detail="User mismatch")

    # 1) Collect storage paths for user's stories (audio files) then delete stories.
    sr = supabase.table("Stories").select("id,storage").eq("user_id", user_id).execute()
    srows = list(sr.data or [])
    storage_paths = []
    for s in srows:
        p = (s.get("storage") or "").strip()
        if p:
            storage_paths.append(p)

    # Remove files from storage (best-effort).
    bucket = (settings.SUPABASE_STORAGE_BUCKET or "").strip()
    if bucket and storage_paths:
        try:
            # Supabase storage remove can take a list of paths.
            supabase.storage.from_(bucket).remove(storage_paths)
        except Exception as e:
            logging.warning("Storage remove failed: %s", e)

    # Delete Stories rows.
    try:
        supabase.table("Stories").delete().eq("user_id", user_id).execute()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to delete stories: {e}")

    # Delete Users row.
    try:
        supabase.table("Users").delete().eq("id", user_id).execute()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to delete user: {e}")

    # Delete Supabase auth user (best-effort).
    try:
        await _delete_supabase_auth_user(token_uid)
    except Exception as e:
        logging.warning("Auth user delete failed: %s", e)

    return {"ok": True, "deleted_user_id": user_id}
