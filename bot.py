"""Deterministic, stateful Vera challenge API."""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Vera merchant assistant", docs_url=None, redoc_url=None)
START = time.monotonic()
LOCK = RLock()
SCOPES = ("category", "merchant", "customer", "trigger")
contexts: dict[tuple[str, str], dict] = {}
conversations: dict[str, dict] = {}
sent: set[tuple[str, str, str | None]] = set()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def error(reason: str, details: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"accepted": False, "reason": reason, "details": details}, status_code=status)


async def json_body(request: Request) -> dict | None:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else None
    except (ValueError, UnicodeDecodeError):
        return None


def get(scope: str, cid: str | None) -> dict | None:
    return contexts.get((scope, cid), {}).get("payload") if cid else None


def label(merchant: dict) -> str:
    identity = merchant.get("identity") or {}
    name = identity.get("owner_first_name") or identity.get("name") or "there"
    if merchant.get("category_slug") == "dentists" and not name.startswith("Dr."):
        name = f"Dr. {name}"
    return name


def clean(value: str) -> str:
    """Repair a few mojibake sequences present in the supplied seed JSON."""
    return value.replace("â‚¹", "₹").replace("â€“", "–").replace("â€™", "’")


def first_item(category: dict, payload: dict) -> dict:
    items = category.get("digest") or []
    wanted = payload.get("top_item_id") or payload.get("digest_item_id") or payload.get("alert_id")
    return next((x for x in items if wanted and x.get("id") == wanted), {})


def pct(value: Any) -> str:
    return f"{abs(float(value)) * 100:g}%"


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    """Use only supplied context. Never assert that an external action was completed."""
    kind = trigger.get("kind", "")
    p = trigger.get("payload") or {}
    ident = merchant.get("identity") or {}
    business = ident.get("name") or "your business"
    person = label(merchant)
    perf = merchant.get("performance") or {}
    item = first_item(category, p)
    title, source = item.get("title"), item.get("source")
    offer = next((clean(x["title"]) for x in merchant.get("offers", []) if x.get("status") == "active" and isinstance(x.get("title"), str)), None)
    place = ident.get("locality") or ident.get("city") or "your area"
    cta = "yes_no"
    rationale = f"{kind} trigger for {business}; uses current merchant and category context."
    if customer:
        name = (customer.get("identity") or {}).get("name") or "there"
        lang = ((customer.get("identity") or {}).get("language_pref") or "").lower()
        greeting = f"Namaste {name}" if "hi" in lang else f"Hi {name}"
        if kind in ("recall_due", "customer_lapsed_soft", "customer_lapsed_hard"):
            due = p.get("due_date")
            slot = (p.get("available_slots") or [{}])[0].get("label")
            detail = f"Your follow-up is due on {due}." if due else f"It's time to check in after your visit to {business}."
            body = f"{greeting}, {business} here. {detail}" + (f" We have {slot} available." if slot else "") + " Reply YES if you'd like us to arrange a visit."
        elif kind == "appointment_tomorrow":
            body = f"{greeting}, {business} here. A visit is scheduled for tomorrow. Please reply CONFIRM if the time still works for you."
        elif kind == "chronic_refill_due":
            meds = ", ".join(p.get("molecule_list") or [])
            date = (p.get("stock_runs_out_iso") or "")[:10]
            body = f"{greeting}, {business} here. Your {meds or 'regular medicines'} may run out" + (f" around {date}" if date else " soon") + ". Reply CONFIRM if you want us to prepare the same refill; please check any dosage changes with your clinician."
        elif kind == "trial_followup":
            slot = (p.get("next_session_options") or [{}])[0].get("label")
            body = f"{greeting}, {business} here. Thanks for trying a session with us." + (f" The next option is {slot}." if slot else "") + " Reply YES if you'd like us to reserve it."
        else:
            body = f"{greeting}, {business} here. Following up on your visit with us. Reply YES if you'd like us to contact you about the next step."
        return {"body": body, "cta": cta, "send_as": "merchant_on_behalf", "suppression_key": trigger.get("suppression_key") or trigger.get("id", ""), "rationale": rationale}

    if kind in ("research_digest", "regulation_change", "cde_opportunity", "supply_alert") and title:
        body = f"{person}, a relevant update for {business}: {title}" + (f" ({source})." if source else ".")
        if kind == "regulation_change" and p.get("deadline_iso"):
            body += f" Deadline: {p['deadline_iso']}."
        if kind == "supply_alert" and p.get("affected_batches"):
            body += f" Check batches {', '.join(p['affected_batches'])}."
        body += " Reply YES and I'll prepare a short checklist for your team."
    elif kind in ("perf_dip", "seasonal_perf_dip", "perf_spike"):
        metric = p.get("metric") or "views"
        change = p.get("delta_pct")
        if change is None:
            change = (perf.get("delta_7d") or {}).get(f"{metric}_pct")
        movement = (f"{'up' if float(change) >= 0 else 'down'} {pct(change)} in {p.get('window', '7d')}" if isinstance(change, (float, int)) else "changed recently")
        body = f"{person}, {business}'s {metric} are {movement}"
        if isinstance(perf.get(metric), (int, float)):
            body += f"; your current {perf.get('window_days', 30)}-day total is {perf[metric]}"
        body += ". " + ("This may be seasonal, so I'd check the trend before changing an offer." if p.get("is_expected_seasonal") else "Want me to draft one profile update to test?")
        if p.get("is_expected_seasonal"):
            cta = "none"
    elif kind == "renewal_due":
        days = p.get("days_remaining", (merchant.get("subscription") or {}).get("days_remaining"))
        amount = p.get("renewal_amount")
        body = f"{person}, {business}'s {p.get('plan') or (merchant.get('subscription') or {}).get('plan', 'plan')} plan has {days} days remaining."
        if amount:
            body += f" Renewal is ₹{amount}; no charge is made by replying."
        body += " Reply YES if you'd like the renewal details."
    elif kind == "winback_eligible":
        body = f"{person}, {business}'s subscription expired {p.get('days_since_expiry', 'some')} days ago."
        if p.get("lapsed_customers_added_since_expiry") is not None:
            body += f" {p['lapsed_customers_added_since_expiry']} customers have lapsed since then."
        body += " Reply YES for a simple restart plan; there is no automatic charge."
    elif kind == "review_theme_emerged":
        theme = (p.get("theme") or "a recurring issue").replace("_", " ")
        count = p.get("occurrences_30d")
        body = f"{person}, {count} recent reviews mention {theme}" if count is not None else f"{person}, recent reviews mention {theme}"
        body += f" at {business}. Want me to draft a response and one practical fix? Reply YES."
    elif kind == "milestone_reached":
        body = f"{person}, {business} is at {p.get('value_now', 'a new high')} {str(p.get('metric', 'reviews')).replace('_', ' ')}."
        if p.get("milestone_value"):
            body += f" {p['milestone_value']} is close."
        body += " Reply YES for a thank-you post draft."
    elif kind == "festival_upcoming":
        body = f"{person}, {p.get('festival', 'a local festival')} is coming up" + (f" on {p['date']}" if p.get("date") else "") + f". For {business}, I can draft a timely post" + (f" featuring your {offer}" if offer else " without inventing an offer") + ". Reply YES to review it."
    elif kind == "competitor_opened":
        body = f"{person}, {p.get('competitor_name', 'a competitor')} opened {p.get('distance_km', 'nearby')} km from {business}." if p.get("distance_km") is not None else f"{person}, a competitor opened near {business}."
        body += f" Your current offer, {offer}, is a concrete point of difference." if offer else " A clear service description could help you stand out."
        body += " Reply YES if you'd like a profile draft."
    elif kind == "gbp_unverified":
        body = f"{person}, {business}'s Google profile is unverified. Verification may be available by {str(p.get('verification_path', 'phone or postcard')).replace('_', ' ')}. Reply YES for a short step-by-step guide."
    elif kind == "active_planning_intent":
        topic = str(p.get("intent_topic", "your idea")).replace("_", " ")
        body = f"{person}, here's a starting outline for {topic}: define the audience, schedule and inclusions, then publish a clear description. Reply with your preferred price and I'll turn it into a draft."
        cta = "open_ended"
    elif kind == "curious_ask_due":
        body = f"{person}, which service is getting the most enquiries this week at {business}? I'll use your answer to shape one relevant Google post."
        cta = "open_ended"
    elif kind == "dormant_with_vera":
        body = f"{person}, it's been {p.get('days_since_last_merchant_message', 'a few')} days since we last spoke. Want a quick check of {business}'s Google profile? Reply YES."
    elif kind == "category_seasonal":
        trends = p.get("trends") or []
        body = f"{person}, seasonal demand is shifting in {place}: {', '.join(str(x).replace('_', ' ') for x in trends[:2]) if trends else 'customer needs are changing'}. Reply YES for a short shelf or content checklist for {business}."
    elif kind == "ipl_match_today":
        body = f"{person}, {p.get('match', 'the match')} is on today" + (f" at {p['venue']}" if p.get("venue") else "") + f". For {business}, I can draft a timely post" + (f" around {offer}" if offer else "") + ". Reply YES to review it."
    else:
        body = f"{person}, an update is available for {business}" + (f" in {place}" if place else "") + ". Reply YES if you'd like a short summary."
    return {"body": body, "cta": cta, "send_as": "vera", "suppression_key": trigger.get("suppression_key") or trigger.get("id", ""), "rationale": rationale}


@app.get("/v1/healthz")
def healthz():
    with LOCK:
        counts = {scope: sum(s == scope for s, _ in contexts) for scope in SCOPES}
    return {"status": "ok", "uptime_seconds": int(time.monotonic() - START), "contexts_loaded": counts}


@app.get("/v1/metadata")
def metadata():
    return {"team_name": os.getenv("TEAM_NAME", "Vera Challenge"), "team_members": [], "model": "deterministic-local-rules", "approach": "Context-grounded trigger composition and intent routing", "contact_email": os.getenv("CONTACT_EMAIL", ""), "version": "1.0.0", "submitted_at": now_iso()}


@app.post("/v1/context")
async def push_context(request: Request):
    data = await json_body(request)
    if data is None:
        return error("invalid_json", "Expected a JSON object")
    scope, cid, version, payload = (data.get(k) for k in ("scope", "context_id", "version", "payload"))
    if scope not in SCOPES:
        return error("invalid_scope", "scope must be category, merchant, customer, or trigger")
    if not isinstance(cid, str) or not cid.strip() or not isinstance(version, int) or isinstance(version, bool) or version < 1 or not isinstance(payload, dict):
        return error("invalid_context", "context_id, positive integer version, and object payload are required")
    field = {"category": "slug", "merchant": "merchant_id", "customer": "customer_id", "trigger": "id"}[scope]
    if payload.get(field) != cid:
        return error("invalid_context", f"payload.{field} must match context_id")
    with LOCK:
        current = contexts.get((scope, cid))
        if current and current["version"] > version:
            return JSONResponse({"accepted": False, "reason": "stale_version", "current_version": current["version"]}, status_code=409)
        if not current or current["version"] < version:
            contexts[(scope, cid)] = {"version": version, "payload": payload}
    return {"accepted": True, "ack_id": f"ack_{cid}_v{version}", "stored_at": now_iso()}


@app.post("/v1/tick")
async def tick(request: Request):
    data = await json_body(request)
    if data is None or not isinstance(data.get("available_triggers"), list) or not isinstance(data.get("now"), str):
        return error("invalid_tick", "now and available_triggers list are required")
    try:
        tick_time = datetime.fromisoformat(data["now"].replace("Z", "+00:00"))
        if tick_time.tzinfo is None:
            raise ValueError()
    except ValueError:
        return error("invalid_tick", "now must be an ISO 8601 timestamp with timezone")
    actions = []
    with LOCK:
        for tid in data["available_triggers"]:
            if len(actions) >= 20:
                break
            if not isinstance(tid, str):
                continue
            trigger = get("trigger", tid)
            if not trigger:
                continue
            mid = trigger.get("merchant_id") or (trigger.get("payload") or {}).get("merchant_id")
            merchant = get("merchant", mid)
            category = get("category", merchant.get("category_slug")) if merchant else None
            cid = trigger.get("customer_id")
            customer = get("customer", cid) if cid else None
            if not merchant or not category or (trigger.get("scope") == "customer" and (not customer or customer.get("merchant_id") != mid)):
                continue
            try:
                expiry = datetime.fromisoformat(trigger.get("expires_at", "").replace("Z", "+00:00"))
                if tick_time > expiry:
                    continue
            except ValueError:
                pass
            if customer and not (customer.get("preferences") or {}).get("reminder_opt_in", True):
                continue
            key = (mid, trigger.get("suppression_key") or tid, cid)
            if key in sent:
                continue
            composed = compose(category, merchant, trigger, customer)
            conv_id = f"conv_{len(conversations) + 1}_{tid}"
            action = {"conversation_id": conv_id, "merchant_id": mid, "customer_id": cid, "send_as": composed["send_as"], "trigger_id": tid, "template_name": "vera_context_v1" if not customer else "merchant_reminder_v1", "template_params": [label(merchant) if not customer else (customer.get("identity") or {}).get("name", "there"), merchant.get("identity", {}).get("name", "")], **composed}
            actions.append(action)
            sent.add(key)
            conversations[conv_id] = {"merchant_id": mid, "customer_id": cid, "trigger_id": tid, "last_body": action["body"], "turns": 0, "ended": False}
    return {"actions": actions}


@app.post("/v1/reply")
async def reply(request: Request):
    data = await json_body(request)
    if data is None or not isinstance(data.get("conversation_id"), str) or not isinstance(data.get("message"), str) or not data["message"].strip() or data.get("from_role") not in ("merchant", "customer"):
        return error("invalid_reply", "conversation_id, from_role, and nonempty message are required")
    message = data["message"].strip()
    low = message.lower()
    with LOCK:
        state = conversations.setdefault(data["conversation_id"], {"merchant_id": data.get("merchant_id"), "customer_id": data.get("customer_id"), "trigger_id": None, "last_body": "", "turns": 0, "ended": False})
        if state["ended"]:
            return {"action": "end", "rationale": "Conversation already ended."}
        if state["merchant_id"] and data.get("merchant_id") and state["merchant_id"] != data["merchant_id"]:
            return error("invalid_reply", "merchant_id does not match conversation")
        state["turns"] += 1
        if re.search(r"\b(stop|unsubscribe|not interested|spam|don't message|do not message|useless)\b", low):
            state["ended"] = True
            return {"action": "end", "rationale": "User opted out or expressed disinterest."}
        if re.search(r"thank you for contacting|we will (get back|respond)|automated (reply|message|assistant)|out of office|business hours|our team will respond", low):
            state["ended"] = True
            return {"action": "end", "rationale": "Detected a WhatsApp Business automatic response."}
        if re.search(r"\b(later|busy|tomorrow|next week)\b", low):
            return {"action": "wait", "wait_seconds": 1800, "rationale": "User asked for more time."}
        if re.search(r"\b(gst|tax filing|income tax)\b", low):
            return {"action": "send", "body": "I can help with your business profile and customer messages, but I can't file taxes. Please check with your accountant.", "cta": "none", "rationale": "Answered the off-topic request without claiming tax expertise."}
        merchant = get("merchant", state["merchant_id"]) or {}
        trigger = get("trigger", state["trigger_id"]) or {}
        kind = trigger.get("kind", "")
        if re.search(r"\b(yes|ok|okay|sure|confirm|proceed|do it|let's do|lets do|send|book)\b", low):
            if kind in ("research_digest", "regulation_change", "cde_opportunity", "supply_alert"):
                cat = get("category", merchant.get("category_slug")) or {}
                item = first_item(cat, trigger.get("payload") or {})
                detail = item.get("summary") or item.get("title") or "the update in your context"
                source = item.get("source")
                body = f"Here is the summary: {detail}" + (f" Source: {source}." if source else ".")
            elif state["customer_id"]:
                body = "Thanks for confirming. I've recorded your interest; the team will contact you to finalize the details."
            elif kind in ("renewal_due", "winback_eligible"):
                body = "Here are the next steps: review your plan details in your merchant account, then confirm renewal there. Replying here has not charged you."
            else:
                business = (merchant.get("identity") or {}).get("name") or "your business"
                body = f"Here is a draft for {business}: share the specific service and why it helps local customers. Please confirm the details before publishing; nothing has been posted yet."
            state["last_body"] = body
            return {"action": "send", "body": body, "cta": "none", "rationale": "User accepted; supplied an immediate next step without claiming an external action."}
        if state["turns"] >= 3:
            state["ended"] = True
            return {"action": "end", "rationale": "Conversation has reached a natural stopping point."}
        return {"action": "send", "body": "I can help with the profile update or the specific reminder I sent. Tell me which detail you'd like clarified.", "cta": "open_ended", "rationale": "Clarifies the user's question while staying within available context."}


@app.post("/v1/teardown")
def teardown():
    with LOCK:
        contexts.clear()
        conversations.clear()
        sent.clear()
    return {"cleared": True}
