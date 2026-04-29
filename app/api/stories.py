"""Stories endpoint: list stories for a user; generate story theme and story via Claude."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, model_validator

from app.core.claude import generate_story, generate_deepen_story
from app.core.config import CATEGORIES, ENERGY_WORDS
from app.core.db_utils import safe_partial_update
from app.core.supabase_client import get_supabase

router = APIRouter(prefix="/stories", tags=["stories"])

class GenerateStoryRequest(BaseModel):
    user_id: int = Field(..., description="User who owns this story")
    name: str = Field(..., min_length=1, description="User's first name")
    location: str = Field(..., min_length=1, description="Where their dream life takes place (city or country)")
    energyWord: str = Field(..., description="Energy word: Powerful, Peaceful, Abundant, Grateful, Confident")
    desireCategory: str = Field(..., description="Category: Love, Money, Career, Health, Home")
    desireDescription: str = Field(..., min_length=1, description="User's description, past tense")
    lovedOne: str | None = Field(None, description="Someone they love (optional)")
    timezone: str | None = Field(None, description="User's local IANA timezone, e.g. America/New_York")

    @model_validator(mode="after")
    def check_energy_and_category(self):
        if self.energyWord not in ENERGY_WORDS:
            raise ValueError(f"energyWord must be one of: {ENERGY_WORDS}")
        if self.desireCategory not in CATEGORIES:
            raise ValueError(f"desireCategory must be one of: {CATEGORIES}")
        return self


class DeepenStoryRequest(BaseModel):
    """Request body for generating a deepening continuation of an existing story."""

    user_id: int = Field(..., description="User who owns the original story")
    story_id: int = Field(..., description="Original story id to deepen")
    name: str = Field(..., min_length=1, description="User's first name")
    location: str = Field(..., min_length=1, description="Where their dream life takes place (city or country)")
    energyWord: str = Field(..., description="Energy word: Powerful, Peaceful, Abundant, Grateful, Confident")
    lovedOne: str | None = Field(None, description="Someone they love (optional)")
    timezone: str | None = Field(None, description="User's local IANA timezone, e.g. America/New_York")

    @model_validator(mode="after")
    def check_energy(self):
        if self.energyWord not in ENERGY_WORDS:
            raise ValueError(f"energyWord must be one of: {ENERGY_WORDS}")
        return self


@router.get("")
async def get_stories(user_id: str = Query(..., description="Filter stories by this user ID")):
    supabase = get_supabase()
    try:
        uid = int(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="user_id must be an integer")

    # Use service_role key in .env so RLS doesn't return empty; only non-deleted stories; only stories with voice_id set (not null, not empty string)
    r = supabase.table("Stories").select("*").eq("user_id", uid).or_("is_deleted.eq.false,is_deleted.is.null").execute()
    rows = list(r.data or [])
    rows = [s for s in rows if (s.get("voice_id") or "").strip()]
    if not rows:
        return {"stories": []}

    desire_ids = list({s["desire_id"] for s in rows if s.get("desire_id") is not None})
    name_by_id = {}
    if desire_ids:
        dr = supabase.table("Desires").select("id, desireCategory").in_("id", desire_ids).execute()
        name_by_id = {d["id"]: d.get("desireCategory") for d in (dr.data or [])}

    for row in rows:
        row["desire_name"] = name_by_id.get(row.get("desire_id"))

    return {"stories": rows}


@router.delete("/{story_id}")
async def delete_story(
    story_id: int,
    user_id: int | None = Query(None, description="Optional: verify the story belongs to this user"),
):
    """Soft-delete a story by id (sets is_deleted). Optionally pass user_id to ensure ownership."""
    supabase = get_supabase()
    r = supabase.table("Stories").select("id", "user_id").eq("id", story_id).or_("is_deleted.eq.false,is_deleted.is.null,voice_id.is.null").execute()
    rows = list(r.data or [])
    if not rows:
        raise HTTPException(status_code=404, detail="Story not found")
    row = rows[0]
    if user_id is not None:
        story_user_id = row.get("user_id") or row.get("userId")
        if story_user_id != user_id:
            raise HTTPException(status_code=403, detail="Story does not belong to this user")
    try:
        supabase.table("Stories").update({"is_deleted": True}).eq("id", story_id).execute()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to delete story: {e!s}")
    return {"ok": True, "story_id": story_id}


def _tzinfo_from_user_timezone(value: str | None):
    """Convert an IANA timezone or UTC offset string into tzinfo; fall back to UTC."""
    v = (value or "").strip()
    if not v:
        logging.info("[stories.limit] no timezone provided; falling back to UTC")
        return timezone.utc, "UTC"

    if v.upper() == "UTC":
        return timezone.utc, "UTC"

    if v.upper().startswith("UTC") and len(v) >= 4:
        rest = v[3:].strip()
        if rest:
            sign = rest[0]
            if sign in {"+", "-"}:
                hm = rest[1:]
                if ":" in hm:
                    h_s, m_s = hm.split(":", 1)
                else:
                    h_s, m_s = hm, "0"
                try:
                    hours = int(h_s)
                    minutes = int(m_s)
                    if 0 <= hours <= 23 and 0 <= minutes <= 59:
                        delta = timedelta(hours=hours, minutes=minutes)
                        if sign == "-":
                            delta = -delta
                        return timezone(delta), v
                except ValueError:
                    pass

    try:
        return ZoneInfo(v), v
    except ZoneInfoNotFoundError:
        logging.info("[stories.limit] invalid timezone=%r; falling back to UTC", v)
        return timezone.utc, "UTC"
    except Exception:
        logging.exception("[stories.limit] failed to resolve timezone=%r; falling back to UTC", v)
        return timezone.utc, "UTC"


def _local_day_window_utc(user_timezone: str | None) -> tuple[str, str, str]:
    tz, resolved_timezone = _tzinfo_from_user_timezone(user_timezone)
    print(f"tz: {tz}, resolved_timezone: {resolved_timezone}")
    now_utc = datetime.now(timezone.utc)
    print(f"now_utc: {now_utc}")
    local_now = now_utc.astimezone(tz)
    print(f"local_now: {local_now}")
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    print(f"local_start: {local_start}, local_end: {local_end}")
    start_utc = local_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    end_utc = local_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    print(f"start_utc: {start_utc}, end_utc: {end_utc}")
    logging.info(
        "[stories.limit] timezone=%s local_now=%s start_utc=%s end_utc=%s",
        resolved_timezone,
        local_now.isoformat(),
        start_utc,
        end_utc,
    )
    return start_utc, end_utc, resolved_timezone


def _get_user_timezone(supabase, user_id: int, request_timezone: str | None = None) -> tuple[str | None, dict | None]:
    user_row = supabase.table("Users").select("timezone").eq("id", user_id).execute()
    user_data = list(user_row.data or [])
    user_record = user_data[0] if user_data else None
    stored_timezone = (user_record or {}).get("timezone")
    request_timezone = (request_timezone or "").strip() or None
    stored_timezone_clean = (stored_timezone or "").strip() or None

    if request_timezone:
        _, resolved = _tzinfo_from_user_timezone(request_timezone)
        if resolved != "UTC" or request_timezone.upper() == "UTC":
            if request_timezone != stored_timezone_clean:
                try:
                    supabase.table("Users").update({"timezone": request_timezone}).eq("id", user_id).execute()
                    logging.info(
                        "[stories.limit] persisted timezone user_id=%s timezone=%s previous=%s",
                        user_id,
                        request_timezone,
                        stored_timezone_clean,
                    )
                except Exception:
                    logging.exception("[stories.limit] failed to persist timezone user_id=%s", user_id)
            return request_timezone, user_record
        logging.info("[stories.limit] request timezone invalid user_id=%s timezone=%r", user_id, request_timezone)

    return stored_timezone_clean, user_record


def _enforce_daily_story_limit(supabase, user_id: int, request_timezone: str | None = None, source: str = "generate") -> None:
    user_timezone, _ = _get_user_timezone(supabase, user_id, request_timezone)
    today_start, tomorrow_start, resolved_timezone = _local_day_window_utc(user_timezone)
    print(f"today_start: {today_start}, tomorrow_start: {tomorrow_start}, resolved_timezone: {resolved_timezone}")
    print(f"user_timezone: {user_timezone}")

    r_today = (
        supabase.table("Stories")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .gte("created_at", today_start)
        .lt("created_at", tomorrow_start)
        .or_("is_deleted.eq.false,is_deleted.is.null")
        .execute()
    )

    print(f"r_today: {r_today}")
    count_today = getattr(r_today, "count", None)
    if count_today is None:
        count_today = len(r_today.data or []) if r_today.data is not None else 0
    
    if (count_today or 0) >= 1 and user_id != 257 and user_id != 237:
        raise HTTPException(
            status_code=403,
            detail=(
                "Users can generate up to 1 story per day. "
                f"Your day resets at midnight in {resolved_timezone}."
            ),
        )

def _get_desire_id_by_name(supabase, category: str) -> int:
    """Look up Desires.id by Desires.desireCategory. Raises if not found."""
    r = supabase.table("Desires").select("id").eq("desireCategory", category).execute()
    rows = list(r.data or [])
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No desire found for category {category!r}. Add a row in Desires with desireCategory={category!r}.",
        )
    row = rows[0]
    desire_id = row.get("id") or row.get("Id")
    if desire_id is None:
        raise HTTPException(status_code=500, detail="Desires row missing id")
    return int(desire_id)


@router.post("/generate")
async def generate_story_content(body: GenerateStoryRequest):
    logging.info(
        "[stories.generate] start user_id=%s energyWord=%s desireCategory=%s",
        body.user_id,
        body.energyWord,
        body.desireCategory,
    )
    user_id = body.user_id
    name = body.name
    location = body.location
    energyWord = body.energyWord
    desireCategory = body.desireCategory
    desireDescription = body.desireDescription
    lovedOne = body.lovedOne
    timezone = body.timezone

    supabase = get_supabase()

    _enforce_daily_story_limit(supabase, user_id, timezone, source="generate")

    desire_id = _get_desire_id_by_name(supabase, desireCategory)
    r = supabase.table("Stories").select("id", "theme", count="exact").eq("user_id", user_id).eq("desire_id", desire_id).or_("is_deleted.eq.false,is_deleted.is.null").order("id").execute()
    rows = list(r.data or [])
    existing_count = r.count if getattr(r, "count", None) is not None else len(rows)
    story_count = existing_count + 1
    previous_story_themes = [
        (s.get("theme") or "").strip()
        for s in rows
        if (s.get("theme") or "").strip()
    ]
    try:
        theme, story, generation_meta = await generate_story(
            name=name,
            location=location,
            energyWord=energyWord,
            desireCategory=desireCategory,
            desireDescription=desireDescription,
            lovedOne=lovedOne,
            storyCount=story_count,
            previousStoryThemes=previous_story_themes,
        )
    except ValueError as e:
        if "ANTHROPIC_API_KEY" in str(e):
            raise HTTPException(status_code=503, detail="Story generation is not configured")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Story generation failed: {e!s}")

    try:
        r = supabase.table("Stories").insert({
            "theme": theme,
            "user_id": user_id,
            "desire_id": desire_id,
            "story": story,
        }).execute()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to store story: {e!s}")

    rows = list(r.data or [])
    created = rows[0] if rows else {}
    created_id = created.get("id")
    if created_id:
        try:
            safe_partial_update(
                table_name="Stories",
                id_field="id",
                record_id=created_id,
                optional_payload={
                    "llm_generation_status": "completed",
                    "llm_generation_error": None,
                    "llm_generation_metadata": generation_meta,
                    "llm_stop_reason": generation_meta.get("stop_reason"),
                },
            )
        except Exception:
            logging.exception("Failed to persist story generation metadata for story %s", created_id)
    return {
        "id": created_id,
        "theme": theme,
        "story": story,
    }
@router.post("/deepen")
async def deepen_story(body: DeepenStoryRequest):
    """Generate a deepening continuation of an existing story. Requires Stories.parent_story_id and Stories.deepening_level columns."""
    supabase = get_supabase()
    user_id = body.user_id
    story_id = body.story_id

    # Load story and verify ownership (include parent_story_id to resolve root)
    r_orig = supabase.table("Stories").select("id", "user_id", "theme", "story", "desire_id", "voice_id", "parent_story_id").eq("id", story_id).or_("is_deleted.eq.false,is_deleted.is.null").execute()
    orig_rows = list(r_orig.data or [])
    if not orig_rows:
        raise HTTPException(status_code=404, detail="Story not found")
    orig = orig_rows[0]
    story_user_id = orig.get("user_id") or orig.get("userId")
    if story_user_id != user_id:
        raise HTTPException(status_code=403, detail="Story does not belong to this user")

    desire_id = orig.get("desire_id")
    if desire_id is None:
        raise HTTPException(status_code=400, detail="Original story has no desire_id")

    # Resolve root story (Option A: always use root for theme and counting so numbering is #1, #2, #3)
    root = orig
    while root.get("parent_story_id") is not None:
        parent_id = root.get("parent_story_id") or root.get("parent_story_Id")
        r_parent = supabase.table("Stories").select("id", "theme", "story", "voice_id", "parent_story_id").eq("id", parent_id).or_("is_deleted.eq.false,is_deleted.is.null").execute()
        parent_rows = list(r_parent.data or [])
        if not parent_rows:
            break
        root = parent_rows[0]
    root_id = root.get("id") or root.get("Id")
    original_theme = (root.get("theme") or "").strip() or "Manifestation"
    root_story_text = (root.get("story") or "").strip()

    # Get desire category for prompt
    dr = supabase.table("Desires").select("desireCategory").eq("id", desire_id).execute()
    desire_rows = list(dr.data or [])
    original_desire_category = desire_rows[0].get("desireCategory", "Life") if desire_rows else "Life"
    # Existing deepenings under the root (so count is 1, 2, 3...)
    r_deepen = supabase.table("Stories").select("id", "story", "deepening_level").eq("parent_story_id", root_id).or_("is_deleted.eq.false,is_deleted.is.null").execute()
    deepen_rows = list(r_deepen.data or [])
    def _level(row):
        v = row.get("deepening_level") or row.get("deepeningLevel") or 0
        return int(v) if v is not None else 0
    deepen_rows.sort(key=_level)
    previous_story_text = (deepen_rows[-1].get("story") or "").strip() if deepen_rows else root_story_text
    deepening_count = len(deepen_rows) + 1

    _enforce_daily_story_limit(supabase, user_id, body.timezone, source="deepen")

    try:
        theme, story, generation_meta = await generate_deepen_story(
            user_name=body.name,
            location=body.location,
            energy_word=body.energyWord,
            loved_one_name=body.lovedOne or "Not provided",
            original_desire_category=original_desire_category,
            original_theme=original_theme,
            previous_story_text=previous_story_text or "(No previous story)",
            deepening_count=deepening_count,
        )
    except ValueError as e:
        if "ANTHROPIC_API_KEY" in str(e):
            raise HTTPException(status_code=503, detail="Story generation is not configured")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Deepen story generation failed: {e!s}")

    orig_voice_id = (root.get("voice_id") or root.get("voiceId") or "").strip()
    insert_payload = {
        "theme": theme,
        "user_id": user_id,
        "desire_id": desire_id,
        "story": story,
        "parent_story_id": root_id,
        "deepening_level": deepening_count,
    }
    if orig_voice_id:
        insert_payload["voice_id"] = orig_voice_id
    try:
        r = supabase.table("Stories").insert(insert_payload).execute()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to store deepening story: {e!s}")

    rows = list(r.data or [])
    created = rows[0] if rows else {}
    new_story_id = created.get("id")
    if new_story_id:
        try:
            safe_partial_update(
                table_name="Stories",
                id_field="id",
                record_id=new_story_id,
                optional_payload={
                    "llm_generation_status": "completed",
                    "llm_generation_error": None,
                    "llm_generation_metadata": generation_meta,
                    "llm_stop_reason": generation_meta.get("stop_reason"),
                },
            )
        except Exception:
            logging.exception("Failed to persist deepening generation metadata for story %s", new_story_id)

    # Audio: do not block the HTTP response on TTS (avoids nginx/client timeouts). The mobile app
    # calls POST /api/voice/... generate after deepen; other clients should do the same.

    return {
        "id": new_story_id,
        "theme": theme,
        "story": story,
        "deepening_level": deepening_count,
        "parent_story_id": root_id,
    }
