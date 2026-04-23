"""RevenueCat webhook: receive subscription events and update Users (rc_*, subscription_provider)."""

import logging
from typing import Any

from fastapi import APIRouter, Request, HTTPException, Header

from app.core.config import settings
from app.core.supabase_client import get_supabase

router = APIRouter(prefix="/revenuecat", tags=["revenuecat"])

# Event types that mean the user has an active subscription
RC_ACTIVE_TYPES = {
    "INITIAL_PURCHASE",
    "RENEWAL",
    "UNCANCELLATION",
    "PRODUCT_CHANGE",
    "SUBSCRIPTION_EXTENDED",
    "TEMPORARY_ENTITLEMENT_GRANT",
    "REFUND_REVERSED",
}
# Event types that mean subscription is no longer active
RC_INACTIVE_TYPES = {"CANCELLATION", "EXPIRATION"}
RC_BILLING_ISSUE = "BILLING_ISSUE"
RC_PAUSED = "SUBSCRIPTION_PAUSED"


def _get_app_user_id_from_payload(body: dict) -> str | None:
    """Extract app user id from webhook body. TRANSFER uses transferred_to."""
    if body.get("type") == "TRANSFER":
        to_ids = body.get("transferred_to") or []
        return to_ids[0] if to_ids else None
    return body.get("app_user_id")


def _rc_status_from_event(event_type: str) -> str:
    if event_type in RC_ACTIVE_TYPES:
        return "active"
    if event_type in RC_INACTIVE_TYPES:
        return "expired"
    if event_type == RC_BILLING_ISSUE:
        return "billing_issue"
    if event_type == RC_PAUSED:
        return "paused"
    return event_type.lower() if event_type else "unknown"


@router.post("/webhook")
async def revenuecat_webhook(
    request: Request,
    authorization: str | None = Header(None, alias="Authorization"),
):
    """
    RevenueCat webhook: verify Authorization header, then update Users.rc_* and subscription_provider.
    Configure webhook URL: https://dev.alreadydone1.app/api/revenuecat/webhook
    Set Authorization header in RevenueCat dashboard to your REVENUECAT_WEBHOOK_AUTHORIZATION value.
    """
    expected = (settings.REVENUECAT_WEBHOOK_AUTHORIZATION or "").strip()
    if not expected:
        raise HTTPException(status_code=503, detail="RevenueCat webhook authorization not configured")

    # Accept "Bearer <secret>" or raw "<secret>"
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if token != expected:
        raise HTTPException(status_code=401, detail="Invalid authorization")

    try:
        body: dict[str, Any] = await request.json()
    except Exception as e:
        logging.warning("RevenueCat webhook invalid JSON: %s", e)
        raise HTTPException(status_code=400, detail="Invalid JSON")

    event_type = (body.get("type") or "").strip()
    app_user_id = _get_app_user_id_from_payload(body)
    if not app_user_id:
        # TEST or other events may have no user; acknowledge
        return {"received": True}

    try:
        user_id = int(app_user_id)
    except (TypeError, ValueError):
        logging.warning("RevenueCat webhook invalid app_user_id: %s", app_user_id)
        return {"received": True}

    rc_status = _rc_status_from_event(event_type)
    product_id = body.get("product_id") or body.get("new_product_id") or ""
    if isinstance(product_id, list):
        product_id = product_id[0] if product_id else ""
    product_id = str(product_id).strip() if product_id else ""

    update_payload: dict[str, Any] = {
        "rc_subscription_status": rc_status,
        "subscription_provider": "revenuecat",
    }
    if product_id:
        update_payload["rc_subscription_plan"] = product_id
    # Keep rc_customer_id in sync with app_user_id so we can look up by it
    update_payload["rc_customer_id"] = str(app_user_id).strip()

    supabase = get_supabase()
    try:
        supabase.table("Users").update(update_payload).eq("id", user_id).execute()
    except Exception as e:
        logging.exception("RevenueCat webhook update user %s failed: %s", user_id, e)
        raise HTTPException(status_code=502, detail="Failed to update user")

    return {"received": True}
