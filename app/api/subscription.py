import logging
from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, Field

import stripe
from app.core.config import settings
from app.core.supabase_client import get_supabase

router = APIRouter(prefix="/subscription", tags=["subscription"])

# Plan identifiers matching paywall: monthly, annual
PLAN_ANNUAL = "annual"
PLAN_MONTHLY = "monthly"

def _get_price_id(plan: str) -> str:
    if plan == PLAN_ANNUAL:
        return settings.STRIPE_PRICE_ID_ANNUAL
    if plan == PLAN_MONTHLY:
        return settings.STRIPE_PRICE_ID_MONTHLY
    raise ValueError(f"plan must be {PLAN_MONTHLY!r} or {PLAN_ANNUAL!r}")


def _plan_from_price_id(price_id: str) -> str:
    if price_id == settings.STRIPE_PRICE_ID_ANNUAL:
        return PLAN_ANNUAL
    if price_id == settings.STRIPE_PRICE_ID_MONTHLY:
        return PLAN_MONTHLY
    return "unknown"


def datetime_from_timestamp(ts: int) -> "datetime":
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _fetch_subscription_from_stripe(subscription_id: str) -> dict | None:
    """Retrieve subscription from Stripe; return dict with status, plan, trial_end (iso) or None if not found/invalid."""
    if not settings.STRIPE_SECRET_KEY:
        return None
    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        sub = stripe.Subscription.retrieve(subscription_id)
    except stripe.StripeError as e:
        logging.warning("Stripe Subscription.retrieve %s: %s", subscription_id, e)
        return None
    status = (getattr(sub, "status", None) or sub.get("status") or "").lower()
    trial_end_ts = getattr(sub, "trial_end", None) or sub.get("trial_end")
    trial_end_iso = datetime_from_timestamp(trial_end_ts).isoformat() if trial_end_ts else None
    items = (sub.get("items") or {}).get("data") or []
    price_id = (items[0].get("price") or {}).get("id") if items else None
    plan = _plan_from_price_id(price_id) if price_id else "unknown"
    return {"status": status, "plan": plan, "trial_end": trial_end_iso}

class CreateSetupIntentRequest(BaseModel):
    user_id: int = Field(..., description="App user id")
    customer_email: str | None = Field(None, description="Optional; required if user has no Stripe customer yet (for creating one)")


@router.post("/setup-intent")
async def create_setup_intent(body: CreateSetupIntentRequest):
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Stripe is not configured")

    stripe.api_key = settings.STRIPE_SECRET_KEY
    supabase = get_supabase()

    r = supabase.table("Users").select("stripe_customer_id").eq("id", body.user_id).execute()
    rows = list(r.data or [])
    if not rows:
        raise HTTPException(status_code=404, detail="User not found")
    row = rows[0]
    customer_id = row.get("stripe_customer_id") or row.get("stripe_customer_Id")

    if not customer_id:
        if not body.customer_email or not body.customer_email.strip():
            raise HTTPException(
                status_code=400,
                detail="customer_email required when user has no Stripe customer yet",
            )
        try:
            customer = stripe.Customer.create(
                email=body.customer_email.strip(),
                metadata={"user_id": str(body.user_id)},
            )
            customer_id = customer.id
            supabase.table("Users").update({"stripe_customer_id": customer_id}).eq("id", body.user_id).execute()
        except stripe.StripeError as e:
            logging.exception("Stripe Customer.create error: %s", e)
            raise HTTPException(status_code=502, detail="Failed to create customer")
    else:
        customer_id = str(customer_id)

    try:
        setup_intent = stripe.SetupIntent.create(
            customer=customer_id,
            payment_method_types=["card"],
            usage="off_session",
            metadata={"user_id": str(body.user_id)},
        )
    except stripe.StripeError as e:
        logging.exception("Stripe SetupIntent.create error: %s", e)
        raise HTTPException(status_code=502, detail="Failed to create SetupIntent")

    try:
        supabase.table("Users").update({"setup_intent_id": setup_intent.id}).eq("id", body.user_id).execute()
    except Exception as e:
        logging.warning("Failed to store setup_intent_id for user %s: %s", body.user_id, e)

    return {
        "client_secret": setup_intent.client_secret,
        "setup_intent_id": setup_intent.id,
    }

class CreateSubscriptionRequest(BaseModel):
    user_id: int = Field(..., description="App user id")
    plan: str = Field(..., description="monthly or annual")
    payment_method_id: str | None = Field(None, description="Stripe payment method id (pm_xxx) from Payment Sheet. Omit if passing setup_intent_id.")
    setup_intent_id: str | None = Field(None, description="SetupIntent id (seti_xxx) after Payment Sheet confirmation. Used to resolve payment_method when payment_method_id not sent.")
    customer_email: str | None = Field(None, description="Optional; required if user has no Stripe customer yet (for creating one)")


@router.post("/create")
async def create_subscription(body: CreateSubscriptionRequest):
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Stripe is not configured")
    try:
        price_id = _get_price_id(body.plan)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not price_id:
        raise HTTPException(status_code=503, detail=f"Stripe price not configured for plan {body.plan}")

    stripe.api_key = settings.STRIPE_SECRET_KEY
    supabase = get_supabase()

    # 1) Load user: stripe_customer_id, stripe_subscription_id
    r = supabase.table("Users").select("stripe_customer_id", "stripe_subscription_id").eq("id", body.user_id).execute()
    rows = list(r.data or [])
    if not rows:
        raise HTTPException(status_code=404, detail="User not found")
    row = rows[0]
    customer_id = row.get("stripe_customer_id") or row.get("stripe_customer_Id")
    existing_subscription_id = row.get("stripe_subscription_id") or row.get("stripe_subscription_Id")

    # 2) Resolve payment_method_id: from body, from SetupIntent after Payment Sheet, or existing customer default
    payment_method_id: str | None = (body.payment_method_id or "").strip() or None
    if not payment_method_id and (body.setup_intent_id or "").strip():
        # Frontend sends setup_intent_id after Payment Sheet confirmation; resolve pm from SetupIntent
        try:
            si = stripe.SetupIntent.retrieve((body.setup_intent_id or "").strip())
            pm = getattr(si, "payment_method", None) or (si.get("payment_method") if isinstance(si, dict) else None)
            if pm:
                payment_method_id = pm if isinstance(pm, str) else (getattr(pm, "id", None) or str(pm))
        except stripe.StripeError as e:
            logging.warning("Stripe SetupIntent.retrieve %s: %s", body.setup_intent_id, e)
    if not payment_method_id and customer_id:
        try:
            customer = stripe.Customer.retrieve(customer_id, expand=["invoice_settings.default_payment_method"])
            inv = getattr(customer, "invoice_settings", None)
            default_pm = getattr(inv, "default_payment_method", None) if inv else None
            if default_pm:
                payment_method_id = default_pm if isinstance(default_pm, str) else (getattr(default_pm, "id", None) or str(default_pm))
        except stripe.StripeError as e:
            logging.warning("Stripe Customer.retrieve for default payment method: %s", e)
    if not payment_method_id:
        raise HTTPException(
            status_code=400,
            detail="Provide payment_method_id from Payment Sheet, or complete payment setup first so we can use your saved card.",
        )

    # 3) Get or create Stripe customer
    if not customer_id:
        if not body.customer_email or not body.customer_email.strip():
            raise HTTPException(
                status_code=400,
                detail="customer_email required when user has no Stripe customer yet",
            )
        try:
            customer = stripe.Customer.create(
                email=body.customer_email.strip(),
                metadata={"user_id": str(body.user_id)},
            )
            customer_id = customer.id
            supabase.table("Users").update({"stripe_customer_id": customer_id}).eq("id", body.user_id).execute()
        except stripe.StripeError as e:
            logging.exception("Stripe Customer.create error: %s", e)
            raise HTTPException(status_code=502, detail="Failed to create customer")
    else:
        customer_id = str(customer_id)

    # 4) If user already has a subscription, delete it; then create new one with customer_id, original payment method, and requested plan
    if existing_subscription_id:
        try:
            stripe.Subscription.delete(existing_subscription_id)
        except stripe.StripeError as e:
            logging.warning("Stripe Subscription.delete %s: %s", existing_subscription_id, e)
        # New subscription is created below using customer_id, payment_method_id (original or from body), and plan

    # 5) Attach payment method to customer if not already and set as default
    try:
        stripe.PaymentMethod.attach(payment_method_id, customer=customer_id)
    except stripe.StripeError as e:
        if "already been attached" in (e.user_message or "") or "already attached" in str(e).lower():
            pass
        else:
            logging.exception("Stripe PaymentMethod.attach error: %s", e)
            raise HTTPException(status_code=502, detail="Failed to attach payment method")
    try:
        stripe.Customer.modify(
            customer_id,
            invoice_settings={"default_payment_method": payment_method_id},
        )
    except stripe.StripeError as e:
        logging.exception("Stripe Customer.modify error: %s", e)
        raise HTTPException(status_code=502, detail="Failed to set default payment method")

    # 6) Create new subscription with trial
    try:
        sub = stripe.Subscription.create(
            customer=customer_id,
            items=[{"price": price_id}],
            default_payment_method=payment_method_id,
            trial_period_days=settings.STRIPE_TRIAL_DAYS,
            metadata={"user_id": str(body.user_id)},
        )
    except stripe.StripeError as e:
        logging.exception("Stripe Subscription.create error: %s", e)
        raise HTTPException(status_code=502, detail="Failed to create subscription")

    # 7) Update Users with new subscription info
    items = (sub.get("items") or {}).get("data") or []
    price_id_used = (items[0].get("price") or {}).get("id") if items else None
    plan = _plan_from_price_id(price_id_used)
    raw_status = (sub.get("status") or "trialing").lower()
    status = "trial" if raw_status == "trialing" else raw_status
    trial_end_ts = sub.get("trial_end")
    trial_end_iso = datetime_from_timestamp(trial_end_ts).isoformat() if trial_end_ts else None
    try:
        supabase.table("Users").update({
            "stripe_subscription_id": sub.id,
            "subscription_status": status,
            "subscription_plan": plan,
            "trial_end": trial_end_iso,
        }).eq("id", body.user_id).execute()
    except Exception as e:
        logging.exception("Failed to update Users with subscription: %s", e)

    return {
        "subscription_id": sub.id,
        "status": status,
        "plan": plan,
        "trial_end": trial_end_iso,
    }

@router.get("/status")
async def subscription_status(user_id: int = Query(..., description="App user id")):
    """Return stripe_customer_id, stripe_subscription_id, intent_id (setup_intent_id), subscription_status, subscription_plan. Syncs with Stripe on each call."""
    supabase = get_supabase()
    r = supabase.table("Users").select(
        "id",
        "stripe_customer_id",
        "stripe_subscription_id",
        "setup_intent_id",
        "subscription_status",
        "subscription_plan",
    ).eq("id", user_id).execute()
    rows = list(r.data or [])
    if not rows:
        raise HTTPException(status_code=404, detail="User not found")
    row = rows[0]
    subscription_id = row.get("stripe_subscription_id") or row.get("stripe_subscription_Id")
    intent_id = row.get("setup_intent_id")
    status = row.get("subscription_status") or row.get("Subscription_Status")
    plan = row.get("subscription_plan") or row.get("Subscription_Plan")

    if subscription_id:
        synced = _fetch_subscription_from_stripe(str(subscription_id))
        if synced:
            status = synced["status"]
            plan = synced["plan"]
            try:
                supabase.table("Users").update({
                    "subscription_status": status,
                    "subscription_plan": plan,
                    "trial_end": synced["trial_end"],
                }).eq("id", user_id).execute()
            except Exception as e:
                logging.exception("Failed to sync subscription status to DB: %s", e)

    return {
        "stripe_customer_id": row.get("stripe_customer_id") or row.get("stripe_customer_Id"),
        "stripe_subscription_id": subscription_id,
        "intent_id": intent_id,
        "subscription_status": status,
        "subscription_plan": plan,
    }


class CancelSubscriptionRequest(BaseModel):
    user_id: int = Field(..., description="App user id")


@router.post("/cancel")
async def cancel_subscription_during_trial(body: CancelSubscriptionRequest):
    """Cancel the subscription only if the user is within the 7-day free trial. Fails if not trialing."""
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Stripe is not configured")
    supabase = get_supabase()
    user_id = body.user_id
    r = supabase.table("Users").select("stripe_subscription_id").eq("id", user_id).execute()
    rows = list(r.data or [])
    if not rows:
        raise HTTPException(status_code=404, detail="User not found")
    subscription_id = rows[0].get("stripe_subscription_id") or rows[0].get("stripe_subscription_Id")
    if not subscription_id:
        raise HTTPException(status_code=400, detail="No subscription to cancel")
    stripe.api_key = settings.STRIPE_SECRET_KEY
    synced = _fetch_subscription_from_stripe(str(subscription_id))
    if not synced:
        raise HTTPException(status_code=400, detail="Subscription not found or invalid")
    if synced.get("status") != "trialing":
        raise HTTPException(
            status_code=400,
            detail="Can only cancel during the 7-day free trial. Your trial has ended or subscription is already canceled.",
        )
    try:
        stripe.Subscription.delete(subscription_id)
    except stripe.StripeError as e:
        logging.exception("Stripe Subscription.delete error: %s", e)
        raise HTTPException(status_code=502, detail="Failed to cancel subscription")
    try:
        supabase.table("Users").update({"subscription_status": "canceled"}).eq("id", user_id).execute()
    except Exception as e:
        logging.exception("Failed to update user subscription_status: %s", e)
    return {"ok": True, "message": "Subscription canceled within free trial."}


class ChangePlanRequest(BaseModel):
    user_id: int = Field(..., description="App user id")
    plan: str = Field(..., description="Target plan: monthly or annual")


@router.post("/change-plan")
async def change_plan(body: ChangePlanRequest):
    """
    Change subscription plan (monthly <-> annual) with proration.
    Stripe credits unused time on the current plan and charges for the new plan (rest value).
    """
    if not settings.STRIPE_SECRET_KEY:
        raise HTTPException(status_code=503, detail="Stripe is not configured")
    try:
        new_price_id = _get_price_id(body.plan)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    supabase = get_supabase()
    r = supabase.table("Users").select("stripe_subscription_id").eq("id", body.user_id).execute()
    rows = list(r.data or [])
    if not rows:
        raise HTTPException(status_code=404, detail="User not found")
    subscription_id = rows[0].get("stripe_subscription_id") or rows[0].get("stripe_subscription_Id")
    if not subscription_id:
        raise HTTPException(status_code=400, detail="No active subscription to change")
    stripe.api_key = settings.STRIPE_SECRET_KEY
    try:
        sub = stripe.Subscription.retrieve(subscription_id)
    except stripe.StripeError as e:
        logging.exception("Stripe Subscription.retrieve error: %s", e)
        raise HTTPException(status_code=400, detail="Subscription not found or invalid")
    items = (sub.get("items") or {}).get("data") or []
    if not items:
        raise HTTPException(status_code=400, detail="Subscription has no items")
    pr = items[0].get("price")
    current_price_id = pr.get("id") if isinstance(pr, dict) else (pr if isinstance(pr, str) else None)
    if current_price_id == new_price_id:
        raise HTTPException(status_code=400, detail=f"Already on {body.plan} plan")
    subscription_item_id = items[0].get("id")
    try:
        updated = stripe.Subscription.modify(
            subscription_id,
            items=[{"id": subscription_item_id, "price": new_price_id}],
            proration_behavior="create_prorations",
        )
    except stripe.StripeError as e:
        logging.exception("Stripe Subscription.modify error: %s", e)
        raise HTTPException(status_code=502, detail="Failed to change plan")
    items_after = (updated.get("items") or {}).get("data") or []
    price_id_used = (items_after[0].get("price") or {}).get("id") if items_after else None
    plan = _plan_from_price_id(price_id_used) if price_id_used else body.plan
    status = (updated.get("status") or "active").lower()
    trial_end_ts = updated.get("trial_end")
    trial_end_iso = datetime_from_timestamp(trial_end_ts).isoformat() if trial_end_ts else None
    try:
        supabase.table("Users").update({
            "subscription_status": status,
            "subscription_plan": plan,
            "trial_end": trial_end_iso,
        }).eq("id", body.user_id).execute()
    except Exception as e:
        logging.exception("Failed to update user after change plan: %s", e)
    return {
        "ok": True,
        "subscription_id": subscription_id,
        "plan": plan,
        "status": status,
        "trial_end": trial_end_iso,
        "message": "Plan changed with proration (rest value applied).",
    }


@router.post("/webhook")
async def stripe_webhook(request: Request):

    if not settings.STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook secret not configured")
    payload = await request.body()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(
            payload, sig, settings.STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")
    except stripe.SignatureVerificationError as e:
        raise HTTPException(status_code=400, detail=f"Invalid signature: {e}")

    supabase = get_supabase()

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("client_reference_id")
        if not user_id:
            logging.warning("checkout.session.completed missing client_reference_id")
            return {"received": True}
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            logging.warning("checkout.session.completed invalid user_id: %s", user_id)
            return {"received": True}
        subscription_id = session.get("subscription")
        customer_id = session.get("customer")
        if not subscription_id or not customer_id:
            logging.warning("checkout.session.completed missing subscription or customer")
            return {"received": True}
        # Fetch subscription for trial_end and plan
        stripe.api_key = settings.STRIPE_SECRET_KEY
        try:
            sub = stripe.Subscription.retrieve(subscription_id)
        except stripe.StripeError as e:
            logging.exception("Failed to retrieve subscription %s: %s", subscription_id, e)
            return {"received": True}
            
        trial_end_ts = getattr(sub, "trial_end", None) or sub.get("trial_end")
        items = (sub.get("items") or {}).get("data") or []
        price_id = (items[0].get("price") or {}).get("id") if items else None
        plan = _plan_from_price_id(price_id) if price_id else "unknown"
        status = (getattr(sub, "status", None) or sub.get("status") or "active").lower()
        trial_end_iso = datetime_from_timestamp(trial_end_ts).isoformat() if trial_end_ts else None
        try:
            supabase.table("Users").update({
                "stripe_customer_id": customer_id,
                "stripe_subscription_id": subscription_id,
                "subscription_status": status,
                "subscription_plan": plan,
                "trial_end": trial_end_iso,
            }).eq("id", uid).execute()
        except Exception as e:
            logging.exception("Failed to update Users with subscription: %s", e)
        return {"received": True}

    if event["type"] == "customer.subscription.created":
        sub = event["data"]["object"]
        subscription_id = sub["id"]
        customer_id = sub.get("customer")
        trial_end_ts = sub.get("trial_end")
        items = (sub.get("items") or {}).get("data") or []
        price_id = (items[0].get("price") or {}).get("id") if items else None
        plan = _plan_from_price_id(price_id) if price_id else "unknown"
        raw_status = (sub.get("status") or "trialing").lower()
        status = "trial" if raw_status == "trialing" else raw_status
        trial_end_iso = datetime_from_timestamp(trial_end_ts).isoformat() if trial_end_ts else None
        try:
            supabase.table("Users").update({
                "stripe_subscription_id": subscription_id,
                "subscription_status": status,
                "subscription_plan": plan,
                "trial_end": trial_end_iso,
            }).eq("stripe_customer_id", customer_id).execute()
        except Exception as e:
            logging.exception("Failed to update Users on subscription.created: %s", e)
        return {"received": True}

    if event["type"] == "customer.subscription.updated":
        sub = event["data"]["object"]
        subscription_id = sub["id"]
        raw_status = (sub.get("status") or "active").lower()
        status = "trial" if raw_status == "trialing" else raw_status
        trial_end = sub.get("trial_end")
        items = (sub.get("items") or {}).get("data") or []
        price_id = (items[0].get("price") or {}).get("id") if items else None
        plan = _plan_from_price_id(price_id) if price_id else "unknown"
        payload = {"subscription_status": status, "subscription_plan": plan}
        if trial_end is not None:
            payload["trial_end"] = datetime_from_timestamp(trial_end).isoformat()
      
        try:
            supabase.table("Users").update(payload).eq("stripe_subscription_id", subscription_id).execute()
        except Exception as e:
            logging.exception("Failed to update subscription: %s", e)
        return {"received": True}

    if event["type"] == "customer.subscription.deleted":
        sub = event["data"]["object"]
        subscription_id = sub["id"]
        try:
            supabase.table("Users").update({
                "subscription_status": "canceled",
            }).eq("stripe_subscription_id", subscription_id).execute()
        except Exception as e:
            logging.exception("Failed to mark subscription canceled: %s", e)
        return {"received": True}

    return {"received": True}
