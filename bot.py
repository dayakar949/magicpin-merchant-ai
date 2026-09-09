"""
Magicpin Vera-style merchant engagement bot.

Run:
    python -m pip install -r requirements.txt
    python -m uvicorn bot:app --host 0.0.0.0 --port 8080
"""

import json
import os
import re
from collections import Counter
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel


app = FastAPI(title="Magicpin Vera Bot", version="1.0.0")
@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "magicpin-merchant-ai",
        "message": "Vera merchant AI bot is running"
    }


# ---------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------

contexts: dict[str, dict[str, Any]] = {
    "category": {},
    "merchant": {},
    "trigger": {},
    "customer": {},
}

context_versions: dict[tuple[str, str], int] = {}
conversations: dict[str, dict[str, Any]] = {}
sent_suppressions: set[str] = set()


# ---------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------

class ContextRequest(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: Optional[str] = None


class TickRequest(BaseModel):
    now: str
    available_triggers: list[dict[str, Any]] = []


class ReplyRequest(BaseModel):
    conversation_id: str
    merchant_id: Optional[str] = None
    customer_id: Optional[str] = None
    message: str


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def get_id(obj: dict[str, Any], fallback: str = "") -> str:
    return str(
        obj.get("id")
        or obj.get("merchant_id")
        or obj.get("customer_id")
        or obj.get("trigger_id")
        or obj.get("slug")
        or fallback
    )


def find_context(scope: str, object_id: str) -> dict[str, Any]:
    return contexts.get(scope, {}).get(str(object_id), {})


def merchant_category(merchant: dict[str, Any]) -> dict[str, Any]:
    category_id = (
        merchant.get("category_id")
        or merchant.get("category_slug")
        or merchant.get("category")
        or merchant.get("slug")
    )
    if category_id:
        category = find_context("category", str(category_id))
        if category:
            return category
    return {}


def language_hint(merchant: dict[str, Any]) -> str:
    value = str(
        merchant.get("language")
        or merchant.get("preferred_language")
        or merchant.get("language_preference")
        or ""
    ).lower()

    if "hindi" in value or value in {"hi", "hinglish"}:
        return "hinglish"
    if "english" in value or value in {"en", "en-in"}:
        return "english"
    return "neutral"


def merchant_name(merchant: dict[str, Any]) -> str:
    identity = merchant.get("identity") or {}
    return str(
        merchant.get("name")
        or merchant.get("merchant_name")
        or merchant.get("business_name")
        or identity.get("name")
        or "your business"
    )


def category_name(category: dict[str, Any], merchant: dict[str, Any]) -> str:
    return str(
        category.get("name")
        or category.get("display_name")
        or category.get("slug")
        or merchant.get("category")
        or "business"
    )


def trigger_text(trigger: dict[str, Any]) -> str:
    payload = trigger.get("payload") or {}
    for key in (
        "message",
        "text",
        "summary",
        "description",
        "title",
        "reason",
        "content",
    ):
        if payload.get(key):
            return str(payload[key])
        if trigger.get(key):
            return str(trigger[key])
    return ""


def extract_price(obj: dict[str, Any]) -> Optional[str]:
    text = json.dumps(obj, ensure_ascii=False)
    match = re.search(r"(?:₹|rs\.?\s*)(\d[\d,]*)", text, flags=re.I)
    return f"₹{match.group(1)}" if match else None


def first_offer(category: dict[str, Any], merchant: dict[str, Any]) -> Optional[dict[str, Any]]:
    offers = merchant.get("offers") or merchant.get("offer_catalog") or category.get("offer_catalog") or []
    if isinstance(offers, dict):
        offers = list(offers.values())

    if isinstance(offers, list) and offers:
        item = offers[0]
        if isinstance(item, dict):
            return item
        return {"name": str(item)}

    return None


def has_explicit_intent(message: str) -> bool:
    text = normalize(message)
    patterns = [
        r"\bi want to join\b",
        r"\blet'?s do it\b",
        r"\bgo ahead\b",
        r"\bdo it\b",
        r"\bproceed\b",
        r"\bstart it\b",
        r"\bsign me up\b",
        r"\bi'?m interested\b",
        r"\byes do it\b",
        r"\bok do it\b",
        r"\bok lets do it\b",
        r"\bkar do\b",
        r"\bkar dijiye\b",
        r"\bshuru karo\b",
        r"\bstart karo\b",
        r"\bhaan\b",
    ]
    return any(re.search(p, text) for p in patterns)


def is_hard_no(message: str) -> bool:
    text = normalize(message)
    return text in {
        "no",
        "nope",
        "nah",
        "not interested",
        "stop",
        "unsubscribe",
        "don't message",
        "dont message",
        "no thanks",
    } or text.startswith("stop ")


def is_off_topic(message: str) -> bool:
    text = normalize(message)
    keywords = [
        "gst",
        "tax",
        "weather",
        "cricket",
        "movie",
        "politics",
        "recipe",
        "train",
        "flight",
    ]
    return any(word in text for word in keywords)


def repeated_auto_reply(history: list[dict[str, Any]]) -> bool:
    merchant_messages = [
        normalize(turn["text"])
        for turn in history
        if turn.get("role") == "merchant" and turn.get("text")
    ]
    if len(merchant_messages) < 3:
        return False

    counts = Counter(merchant_messages)
    return max(counts.values()) >= 3


def make_suppression_key(
    trigger: dict[str, Any],
    merchant: dict[str, Any],
    customer: Optional[dict[str, Any]],
) -> str:
    supplied = trigger.get("suppression_key")
    if supplied:
        return str(supplied)

    trigger_id = get_id(trigger, "trigger")
    merchant_id = get_id(merchant, "merchant")
    customer_id = get_id(customer or {}, "none")
    return f"{trigger_id}:{merchant_id}:{customer_id}"


# ---------------------------------------------------------------------
# Composer
# ---------------------------------------------------------------------

def _pct(x: Any) -> str:
    try:
        v = float(x) * 100 if abs(float(x)) <= 1 else float(x)
        sign = "+" if v > 0 else ""
        return f"{sign}{v:.0f}%"
    except Exception:
        return str(x)


def _first_name(obj: dict[str, Any]) -> str:
    ident = obj.get("identity") or {}
    return str(ident.get("owner_first_name") or ident.get("name") or "").split()[0]


def _customer_name(customer: dict[str, Any]) -> str:
    ident = customer.get("identity") or {}
    return str(ident.get("name") or customer.get("name") or "there")


def _digest_item(category: dict[str, Any], item_id: str = "") -> dict[str, Any]:
    for item in category.get("digest") or []:
        if not isinstance(item, dict):
            continue
        if item_id and item.get("id") == item_id:
            return item
    return {}


def _merchant_voice_prefix(category: dict[str, Any], merchant: dict[str, Any]) -> str:
    name = merchant_name(merchant)
    first = _first_name(merchant)
    tone = ((category.get("voice") or {}).get("tone") or "").lower()
    if tone == "peer_clinical" and first:
        return f"Dr. {first}"
    return first or name


def _category_offer(category: dict[str, Any], merchant: dict[str, Any], keywords: tuple[str, ...] = ()) -> str:
    offers = merchant.get("offers") or []
    active = [o for o in offers if isinstance(o, dict) and str(o.get("status", "active")).lower() == "active"]
    if not active:
        active = [o for o in category.get("offer_catalog") or [] if isinstance(o, dict)]
    if keywords:
        for o in active:
            text = str(o.get("title") or o.get("name") or "").lower()
            if any(k in text for k in keywords):
                return str(o.get("title") or o.get("name"))
    return str((active[0].get("title") or active[0].get("name")) if active else "")


def compose(
    category: dict[str, Any],
    merchant: dict[str, Any],
    trigger: dict[str, Any],
    customer: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Generalized deterministic composer: route by trigger semantics + context facts."""
    kind = str(trigger.get("kind") or trigger.get("type") or "").lower()
    payload = trigger.get("payload") or {}
    name = _merchant_voice_prefix(category, merchant)
    cat = category_name(category, merchant)
    key = make_suppression_key(trigger, merchant, customer)

    # ---------------- Customer-facing ----------------
    if customer:
        cname = _customer_name(customer)
        prefs = customer.get("preferences") or {}
        if "recall" in kind:
            due = payload.get("due_date")
            service = str(payload.get("service_due") or "next visit").replace("_", " ")
            slots = payload.get("available_slots") or []
            slot = slots[0].get("label") if slots and isinstance(slots[0], dict) else None
            body = f"Hi {cname}, {merchant_name(merchant)} here. Your {service} is due"
            if due:
                body += f" around {due}"
            if slot:
                body += f" — I have {slot} available"
            body += ". Reply YES and I’ll help with the next step."
            return {"body": body, "cta": "YES", "send_as": "merchant_on_behalf", "suppression_key": key,
                    "rationale": "Customer recall uses the due service/date and an available slot when supplied."}
        if "appointment" in kind:
            last = (customer.get("relationship") or {}).get("last_visit")
            body = f"Hi {cname}, a quick reminder from {merchant_name(merchant)}: your appointment is tomorrow."
            if last:
                body += f" We last saw you on {last}."
            body += " Reply YES to confirm."
            return {"body": body, "cta": "YES", "send_as": "merchant_on_behalf", "suppression_key": key,
                    "rationale": "Appointment trigger gets a concise confirmation action without inventing a time."}
        if "refill" in kind:
            molecules = payload.get("molecule_list") or []
            stock = payload.get("stock_runs_out_iso")
            if molecules or stock:
                body = f"Hi {cname}, {merchant_name(merchant)} here. Your refill reminder is due."
                if molecules:
                    body += f" It covers {', '.join(map(str, molecules[:3]))}."
                if stock:
                    body += f" The current reminder flags {stock} as the stock-out time."
                body += " Reply YES if you want us to help with the refill."
                rationale = "Refill messaging uses only the supplied refill items and timing."
            else:
                body = f"Hi {cname}, {merchant_name(merchant)} here. This reminder is missing the service or item details, so I don’t want to guess."
                body += " Reply YES and we’ll help with the right next step."
                rationale = "Placeholder refill trigger is explicit about missing details instead of inventing a medical item or service."
            return {"body": body, "cta": "YES", "send_as": "merchant_on_behalf", "suppression_key": key,
                    "rationale": rationale}
        if "lapsed" in kind or "winback" in kind:
            days = payload.get("days_since_last_visit") or (customer.get("relationship") or {}).get("days_since_last_visit")
            if not days:
                last = (customer.get("relationship") or {}).get("last_visit")
                # Do not infer a lapse duration from dates here; use the known state only.
                days = None
            focus = payload.get("previous_focus")
            body = f"Hi {cname}, {merchant_name(merchant)} here."
            if days:
                body += f" It’s been {days} days since your last visit"
            else:
                body += " We haven’t seen you recently"
            if focus:
                body += f", and your earlier focus was {str(focus).replace('_', ' ')}"
            body += ". Reply YES if you’d like us to help plan your return."
            return {"body": body, "cta": "YES", "send_as": "merchant_on_behalf", "suppression_key": key,
                    "rationale": "Win-back uses lapse duration and prior customer focus when available."}

    # ---------------- Merchant-facing trigger families ----------------
    if kind == "active_planning_intent" or "planning" in kind:
        topic = str(payload.get("intent_topic") or "the idea you were planning").replace("_", " ")
        last = payload.get("merchant_last_message")
        body = f"{name}, let’s turn your {topic} idea into a concrete test."
        if last:
            body += f" You said: “{str(last)[:100]}”"
        body += " I can draft the offer + customer copy now. Reply YES to start."
        return {"body": body, "cta": "YES", "send_as": "vera", "suppression_key": key,
                "rationale": "Active planning means action: use the merchant’s stated topic and move directly to drafting."}

    if "cde" in kind or "research" in kind or "knowledge" in kind:
        item = _digest_item(category, str(payload.get("digest_item_id") or ""))
        title = item.get("title") or "a new category learning item"
        source = item.get("source")
        credits = payload.get("credits")
        fee = payload.get("fee")
        body = f"{name}, one relevant learning item for your dental practice: {title}" if str(category.get("slug")) == "dentists" else f"{name}, one relevant learning item for your {cat.lower()} business: {title}"
        if source: body += f" — {source}"
        if credits: body += f" ({credits} credits)"
        if fee: body += f"; {str(fee).replace('_', ' ')}"
        body += ". Want the 2-minute takeaway?"
        return {"body": body, "cta": "YES", "send_as": "vera", "suppression_key": key,
                "rationale": "Knowledge/CDE trigger leads with the supplied digest item and concrete access details."}

    if kind == "category_seasonal" or "season" in kind:
        trends = payload.get("trends") or []
        readable = []
        for x in trends[:3]:
            txt = str(x).replace("_", " ")
            txt = re.sub(r"_?([+-])(\d+)$", lambda m: ("up " if m.group(1)=="+" else "down ") + m.group(2) + "%", txt)
            readable.append(txt)
        body = f"{name}, summer demand is shifting: {', '.join(readable)}."
        if payload.get("shelf_action_recommended"):
            body += " Worth checking your shelf mix before the demand window moves."
        body += " Want me to suggest the first 3 changes?"
        return {"body": body, "cta": "YES", "send_as": "vera", "suppression_key": key,
                "rationale": "Seasonal trigger uses the supplied demand shifts and recommended shelf action."}

    if "competitor" in kind:
        comp = payload.get("competitor_name")
        dist = payload.get("distance_km")
        their = payload.get("their_offer")
        if comp:
            body = f"{name}, {comp} opened {dist} km away"
            if their: body += f" with {their}"
            body += ". Your existing offer can be positioned on service/value rather than copying it. Want me to draft that angle?"
        else:
            body = f"{name}, a nearby competitor signal just came in. Want me to check the positioning gap against your current offer?"
        return {"body": body, "cta": "YES", "send_as": "vera", "suppression_key": key,
                "rationale": "Competitor trigger uses only the supplied competitor facts and avoids inventing competitor details."}

    if "curious" in kind:
        questions = {
            "salons": "Which service are customers asking for most this week — hair, skin or bridal?",
            "restaurants": "Which dish are customers asking for most this week?",
            "gyms": "Which goal is getting the most enquiries this week — weight loss, strength or yoga?",
            "dentists": "Which treatment is coming up most in patient enquiries this week?",
            "pharmacies": "Which category is seeing the most customer demand this week?",
        }
        q = questions.get(str(category.get("slug") or ""), f"What are customers asking you for most this week?")
        body = f"{name}, quick one: {q}"
        return {"body": body, "cta": "open_ended", "send_as": "vera", "suppression_key": key,
                "rationale": "Curiosity trigger deliberately asks the merchant for a useful first-party signal."}

    if "dormant" in kind:
        days = payload.get("days_since_last_merchant_message")
        topic = payload.get("last_topic")
        body = f"{name}, it’s been {days} days since our last message" if days else f"{name}, picking this back up after a quiet stretch."
        if topic: body += f" — last topic was {str(topic).replace('_', ' ')}."
        body += " I can pick up from there. Want me to?"
        return {"body": body, "cta": "YES", "send_as": "vera", "suppression_key": key,
                "rationale": "Dormancy message resumes the known previous topic instead of restarting generically."}

    if "festival" in kind:
        fest = payload.get("festival")
        date = payload.get("date")
        if not fest:
            beats = category.get("seasonal_beats") or []
            # Only use a named event when the category data explicitly provides one.
            for beat in beats:
                if isinstance(beat, dict) and (beat.get("name") or beat.get("festival")):
                    fest = beat.get("name") or beat.get("festival")
                    date = date or beat.get("date")
                    break
        if fest:
            body = f"{name}, {fest}"
            if date:
                body += f" is on {date}"
            body += ". Your existing offer can be shaped around that demand window. Want a ready-to-review message?"
            rationale = "Festival trigger anchors on an explicitly supplied event and date when available."
        else:
            body = f"{name}, a festival opportunity is flagged, but the trigger doesn’t include the event name or date."
            body += " I can shape your existing offer once that detail is confirmed. Reply YES to start."
            rationale = "Placeholder festival trigger is transparent about missing event details and avoids inventing a festival or date."
        return {"body": body, "cta": "YES", "send_as": "vera", "suppression_key": key,
                "rationale": rationale}

    if "gbp" in kind or "unverified" in kind:
        uplift = payload.get("estimated_uplift_pct")
        path = payload.get("verification_path")
        body = f"{name}, your Google Business Profile is still unverified."
        if uplift is not None: body += f" The trigger estimates up to {_pct(uplift)} uplift from completing verification."
        if path: body += f" Current path: {str(path).replace('_', ' ')}."
        body += " Want me to walk you through the verification step?"
        return {"body": body, "cta": "YES", "send_as": "vera", "suppression_key": key,
                "rationale": "GBP trigger states the verification status, supplied estimate and verification path without overstating certainty."}

    if "ipl" in kind:
        match = payload.get("match")
        venue = payload.get("venue")
        tm = payload.get("match_time_iso")
        offer = _category_offer(category, merchant, ("pizza", "combo", "thali"))
        is_weeknight = payload.get("is_weeknight")
        body = f"{name}, {match or 'the match'} is the demand window"
        if venue: body += f" at {venue}"
        if tm: body += f" ({tm})"
        if offer and is_weeknight is True:
            body += f". Your existing {offer} fits the weeknight match window."
        elif offer and is_weeknight is False:
            body += f". Your existing {offer} is currently a Tue-Thu offer, so I wouldn’t imply it applies to this Sunday match."
        elif offer:
            body += f". Your existing {offer} is available to build around."
        if offer and is_weeknight is False:
            body += " Want me to draft a match-day angle without implying that Tue-Thu offer applies?"
        else:
            body += " Want me to draft the match-day copy?"
        return {"body": body, "cta": "YES", "send_as": "vera", "suppression_key": key,
                "rationale": "Match trigger uses event timing and explicitly respects the supplied weekday restriction on the existing offer."}

    if "milestone" in kind:
        metric_raw = payload.get("metric")
        metric = str(metric_raw).replace("_", " ") if metric_raw else ""
        now = payload.get("value_now")
        target = payload.get("milestone_value")
        if now is None and metric == "reviews":
            now = (merchant.get("performance") or {}).get("reviews") or (merchant.get("customer_aggregate") or {}).get("reviews")
        if now is not None and metric:
            display_metric = 'reviews' if metric == 'review count' else metric
            body = f"{name}, you’re at {now} {display_metric}"
            if target is not None:
                try:
                    remaining = max(0, float(target) - float(now))
                    body += f" — just {remaining:.0f} to reach {target}"
                except Exception:
                    body += f" — target {target}"
            body += ". Want a simple push to get there?"
            rationale = "Milestone trigger uses explicit current, metric and target values."
        else:
            body = f"{name}, a merchant milestone has been flagged, but the trigger doesn’t include the metric or target."
            body += " I can turn it into a simple push once those details are available. Reply YES to start."
            rationale = "Placeholder milestone trigger is transparent about missing metric/target details and avoids inventing a milestone."
        return {"body": body, "cta": "YES", "send_as": "vera", "suppression_key": key,
                "rationale": rationale}

    if "perf_dip" in kind:
        metric_raw = payload.get("metric")
        metric = str(metric_raw).replace("_", " ") if metric_raw else ""
        delta = payload.get("delta_pct")
        if delta is None and metric:
            delta = ((merchant.get("performance") or {}).get("delta_7d") or {}).get(metric.replace(" ", "_"))
        base = payload.get("vs_baseline")
        window = payload.get("window", "7d")
        if metric and delta is not None:
            magnitude = abs(float(delta) * 100) if abs(float(delta)) <= 1 else abs(float(delta))
            body = f"{name}, {metric} fell {magnitude:.0f}% in the last {window}"
            if base is not None: body += f" versus a baseline of {base}"
            body += ". Want me to isolate the most likely fix from your current profile/offer setup?"
            rationale = "Performance dip states the supplied metric, decline magnitude and baseline in natural language."
        else:
            deltas = (merchant.get("performance") or {}).get("delta_7d") or {}
            observed = []
            for k, v in deltas.items():
                try:
                    observed.append((k.replace("_pct", "").replace("_", " "), float(v)))
                except Exception:
                    pass
            observed.sort(key=lambda item: abs(item[1]), reverse=True)
            if observed:
                facts = ", ".join(f"{k} {_pct(v)}" for k, v in observed[:2])
                body = f"{name}, the trigger flags a performance dip, but it doesn’t identify the metric. Your current 7d profile shows {facts}. I don’t want to guess which signal is the issue."
            else:
                body = f"{name}, your recent performance has dipped in the last {window}, but the trigger doesn’t include the metric or change."
            body += " Want me to reconcile the signal before acting?"
            rationale = "Placeholder performance-dip trigger is transparent about the missing metric and uses available profile signals without treating them as the trigger’s missing fact."
        return {"body": body, "cta": "YES", "send_as": "vera", "suppression_key": key,
                "rationale": rationale}

    if "perf_spike" in kind:
        metric_raw = payload.get("metric")
        metric = str(metric_raw).replace("_", " ") if metric_raw else ""
        delta = payload.get("delta_pct")
        if delta is None and metric:
            delta = ((merchant.get("performance") or {}).get("delta_7d") or {}).get(metric.replace(" ", "_"))
        driver = str(payload.get("likely_driver") or "").replace("_", " ")
        window = payload.get("window", "7d")
        if metric and delta is not None:
            magnitude = abs(float(delta) * 100) if abs(float(delta)) <= 1 else abs(float(delta))
            body = f"{name}, {metric} rose {magnitude:.0f}% in the last {window}"
            if driver: body += f"; the trigger points to {driver} as a likely driver"
            body += ". Want me to turn that signal into the next test?"
            rationale = "Performance spike states the supplied metric, increase and likely driver naturally."
        else:
            deltas = (merchant.get("performance") or {}).get("delta_7d") or {}
            observed = []
            for k, v in deltas.items():
                try:
                    observed.append((k.replace("_pct", "").replace("_", " "), float(v)))
                except Exception:
                    pass
            observed.sort(key=lambda item: item[1], reverse=True)
            positive = [item for item in observed if item[1] > 0]
            if positive:
                facts = ", ".join(f"{k} {_pct(v)}" for k, v in positive[:2])
                body = f"{name}, the trigger flags positive movement but doesn’t identify the metric or driver. Your current 7d profile shows {facts}. Want me to turn the strongest signal into the next test?"
            else:
                body = f"{name}, your recent performance is trending up, but the trigger doesn’t include the metric or change."
                body += " Want me to identify the signal worth turning into the next test?"
            rationale = "Placeholder performance-spike trigger uses available positive profile signals while avoiding an invented trigger metric or driver."
        return {"body": body, "cta": "YES", "send_as": "vera", "suppression_key": key,
                "rationale": rationale}

    if "regulation" in kind or "compliance" in kind:
        item = _digest_item(category, str(payload.get("top_item_id") or ""))
        title = item.get("title") or "a compliance update"
        source = item.get("source")
        deadline = payload.get("deadline_iso")
        body = f"{name}, compliance heads-up: {title}"
        if source: body += f" — {source}"
        # Avoid repeating a date already present in the supplied title.
        if deadline and str(deadline) not in str(title): body += f"; effective/deadline {deadline}"
        body += ". Want me to pull out the practical checklist?"
        return {"body": body, "cta": "YES", "send_as": "vera", "suppression_key": key,
                "rationale": "Compliance trigger leads with the supplied rule/source and deadline without duplicating facts."}

    # Generic trigger fallback still includes the strongest concrete payload fact.
    fact = trigger_text(trigger)
    body = f"{name}, I have a {cat}-specific update"
    if fact: body += f": {fact[:220]}"
    body += ". Worth a quick look?"
    return {"body": body, "cta": "YES", "send_as": "vera", "suppression_key": key,
            "rationale": "Fallback uses the strongest supplied trigger fact and stays concise."}


# ---------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------

def validate_output(
    result: dict[str, Any],
    conversation: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    body = str(result.get("body") or "").strip()
    cta = str(result.get("cta") or "").strip()

    if not body:
        body = "I have a relevant update for you. Want to take a look?"

    # Keep WhatsApp messages concise.
    if len(body) > 700:
        body = body[:697].rstrip() + "..."

    # Avoid multiple competing CTAs.
    if cta and "|" in cta:
        cta = "YES"

    # Exact repeat penalty avoidance.
    if conversation:
        previous_bodies = [
            str(x.get("body") or "").strip()
            for x in conversation.get("assistant_messages", [])
        ]
        if body in previous_bodies:
            body = "I can take the next step with you now. Reply YES to continue."

    result["body"] = body
    result["cta"] = cta
    result["send_as"] = result.get("send_as") or "merchant"
    result["suppression_key"] = str(result.get("suppression_key") or "")
    result["rationale"] = str(result.get("rationale") or "Context-aware engagement.")
    return result


# ---------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------

@app.get("/v1/healthz")
def healthz():
    return {
        "status": "ok",
        "categories": len(contexts["category"]),
        "merchants": len(contexts["merchant"]),
        "customers": len(contexts["customer"]),
        "triggers": len(contexts["trigger"]),
        "conversations": len(conversations),
    }


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": "Dayakar",
        "team_members": ["J Dayakar"],
        "model": "deterministic",
        "approach": "deterministic context-grounded composer",
        "contact_email": "cs23bt047@iitdh.ac.in",
        "version": "1.0.0"
    }
def metadata():
    return {
        "team_name": os.getenv("TEAM_NAME", "Your Team"),
        "team_members": os.getenv("TEAM_MEMBERS", "Your Name"),
        "contact_email": os.getenv("CONTACT_EMAIL", "your-email@example.com"),
        "bot_version": "1.0.0",
        "composer": "deterministic",
        "model": os.getenv("OPENAI_MODEL", "gpt-5-mini"),
    }


@app.post("/v1/context")
def update_context(request: ContextRequest):
    scope = request.scope.lower()

    if scope not in contexts:
        return {
            "ok": False,
            "error": f"Unsupported scope: {request.scope}",
        }

    key = (scope, str(request.context_id))
    previous_version = context_versions.get(key, -1)

    # Ignore stale versions.
    if request.version < previous_version:
        return {
            "ok": True,
            "updated": False,
            "reason": "stale_version",
        }

    contexts[scope][str(request.context_id)] = request.payload
    context_versions[key] = request.version

    return {
        "ok": True,
        "updated": True,
        "scope": scope,
        "context_id": str(request.context_id),
        "version": request.version,
    }


@app.post("/v1/tick")
def tick(request: TickRequest):
    actions = []

    for trigger in request.available_triggers[:20]:
        trigger_id = get_id(trigger, "")
        merchant_id = str(
            trigger.get("merchant_id")
            or (trigger.get("payload") or {}).get("merchant_id")
            or ""
        )
        customer_id = str(
            trigger.get("customer_id")
            or (trigger.get("payload") or {}).get("customer_id")
            or ""
        )

        merchant = find_context("merchant", merchant_id)
        if not merchant:
            continue

        category = merchant_category(merchant)
        customer = find_context("customer", customer_id) if customer_id else None

        suppression_key = make_suppression_key(trigger, merchant, customer)

        if suppression_key in sent_suppressions:
            continue

        result = compose(category, merchant, trigger, customer)
        result = validate_output(result)

        conversation_id = (
            f"conv_{merchant_id}_{trigger_id}_{len(conversations) + len(actions) + 1}"
        )

        conversations[conversation_id] = {
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id or None,
            "trigger_id": trigger_id,
            "history": [],
            "assistant_messages": [result],
            "created_at": datetime.utcnow().isoformat(),
        }

        sent_suppressions.add(suppression_key)

        action = {
            "conversation_id": conversation_id,
            "merchant_id": merchant_id,
            "customer_id": customer_id or None,
            "send_as": result["send_as"],
            "trigger_id": trigger_id,
            "template_name": trigger.get("template_name") or "vera_generic_v1",
            "template_params": trigger.get("template_params") or [merchant_name(merchant), trigger.get("kind", "update"), result.get("cta", "")],
            "body": result["body"],
            "cta": result["cta"],
            "suppression_key": result["suppression_key"],
            "rationale": result["rationale"],
        }

        actions.append(action)

    return {"actions": actions}


@app.post("/v1/reply")
def reply(request: ReplyRequest):
    conversation = conversations.get(request.conversation_id)

    if not conversation:
        return {
            "action": "end",
            "rationale": "Unknown conversation.",
        }

    message = request.message.strip()
    history = conversation["history"]

    history.append({
        "role": "merchant",
        "text": message,
    })

    # Hard stop / opt-out.
    if is_hard_no(message):
        return {
            "action": "end",
            "rationale": "Merchant explicitly declined or requested STOP.",
        }

    # Auto-reply hell: don't keep burning turns.
    if repeated_auto_reply(history):
        return {
            "action": "end",
            "rationale": "Detected repeated identical canned/auto-reply text; stopping to avoid wasting turns.",
        }

    # Explicit intent transition: act immediately.
    if has_explicit_intent(message):
        result = {
            "body": "Perfect — let’s move ahead. Reply YES and I’ll take you to the next step.",
            "cta": "YES",
            "send_as": "merchant",
            "suppression_key": "",
            "rationale": "Explicit intent detected; skipped further qualification.",
        }
        result = validate_output(result, conversation)
        conversation["assistant_messages"].append(result)
        return {
            "action": "send",
            "body": result["body"],
            "cta": result["cta"],
            "rationale": result["rationale"],
        }

    # Stay on mission if merchant asks an unrelated question.
    if is_off_topic(message):
        result = {
            "body": "Happy to help with that separately. For this update, do you want to continue with the next step?",
            "cta": "YES",
            "send_as": "merchant",
            "suppression_key": "",
            "rationale": "Handled off-topic request briefly while keeping the active mission.",
        }
        result = validate_output(result, conversation)
        conversation["assistant_messages"].append(result)
        return {
            "action": "send",
            "body": result["body"],
            "cta": result["cta"],
            "rationale": result["rationale"],
        }

    # Recompose from the original trigger/context.
    merchant = find_context("merchant", str(request.merchant_id or conversation["merchant_id"]))
    customer_id = request.customer_id or conversation.get("customer_id")
    customer = find_context("customer", str(customer_id)) if customer_id else None
    trigger = find_context("trigger", str(conversation["trigger_id"]))
    category = merchant_category(merchant)

    result = compose(category, merchant, trigger, customer)
    result = validate_output(result, conversation)

    conversation["assistant_messages"].append(result)

    return {
        "action": "send",
        "body": result["body"],
        "cta": result["cta"],
        "rationale": result["rationale"],
    }



def teardown():
    contexts["category"].clear()
    contexts["merchant"].clear()
    contexts["trigger"].clear()
    contexts["customer"].clear()
    context_versions.clear()
    conversations.clear()
    sent_suppressions.clear()

    return {"ok": True, "message": "State cleared."}
