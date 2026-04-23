"""Auth endpoints: signup and login with email + password."""

from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.supabase_client import get_supabase

router = APIRouter(prefix="/auth", tags=["auth"])

# Bcrypt only uses the first 72 bytes of the password; longer inputs can raise.
BCRYPT_MAX_PASSWORD_BYTES = 72


def _password_bytes(password: str) -> bytes:
    """Encode password to bytes, truncating to 72 bytes for bcrypt."""
    raw = password.encode("utf-8")
    if len(raw) > BCRYPT_MAX_PASSWORD_BYTES:
        raw = raw[:BCRYPT_MAX_PASSWORD_BYTES]
    return raw


def _hash_password(password: str) -> str:
    return bcrypt.hashpw(_password_bytes(password), bcrypt.gensalt()).decode("utf-8")


def _verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_password_bytes(plain), hashed.encode("utf-8"))
    except Exception:
        return False


def _create_access_token(user_id: int) -> str:
    if not settings.JWT_SECRET:
        raise ValueError("JWT_SECRET is not set")
    payload = {
        "sub": str(user_id),
        "exp": datetime.now(timezone.utc) + timedelta(days=30),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


class SignupRequest(BaseModel):
    email: str = Field(..., min_length=1, description="User email")
    password: str = Field(..., min_length=8, description="Password (min 8 characters)")
    name: str | None = Field(None, description="Display name (optional)")


class LoginRequest(BaseModel):
    email: str = Field(..., min_length=1, description="User email")
    password: str = Field(..., min_length=1, description="Password")


@router.post("/signup")
async def signup(body: SignupRequest):
    """Create a new user with email and password. Returns user id, email, and access_token."""
    supabase = get_supabase()
    email_lower = body.email.strip().lower()
    r = supabase.table("Users").select("id").eq("email", email_lower).execute()
    if r.data and len(r.data) > 0:
        raise HTTPException(status_code=400, detail="Email already registered")

    hashed = _hash_password(body.password)
    insert_payload = {"email": email_lower, "password": hashed}
    if body.name is not None and body.name.strip():
        insert_payload["name"] = body.name.strip()
    try:
        r = supabase.table("Users").insert(insert_payload).execute()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Failed to create user: {e!s}")

    rows = list(r.data or [])
    if not rows:
        raise HTTPException(status_code=502, detail="User created but no data returned")
    user = rows[0]
    user_id = user.get("id") or user.get("Id")
    if user_id is None:
        raise HTTPException(status_code=502, detail="User created but id missing")
    user_id = int(user_id)

    try:
        access_token = _create_access_token(user_id)
    except ValueError as e:
        raise HTTPException(status_code=503, detail="Auth is not configured")

    return {
        "user_id": user_id,
        "email": email_lower,
        "access_token": access_token,
    }


@router.post("/login")
async def login(body: LoginRequest):
    """Authenticate with email and password. Returns user id, email, and access_token."""
    supabase = get_supabase()
    email_lower = body.email.strip().lower()
    r = supabase.table("Users").select("id", "email", "password").eq("email", email_lower).execute()
    rows = list(r.data or [])
    if not rows:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    user = rows[0]
    stored_hash = user.get("password")
    if not stored_hash:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not _verify_password(body.password, stored_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    user_id = user.get("id") or user.get("Id")
    if user_id is None:
        raise HTTPException(status_code=502, detail="User missing id")
    user_id = int(user_id)

    try:
        access_token = _create_access_token(user_id)
    except ValueError as e:
        raise HTTPException(status_code=503, detail="Auth is not configured")

    return {
        "user_id": user_id,
        "email": email_lower,
        "access_token": access_token,
    }
