"""Contract smoke test against a running local or public server."""
import json
import os
from urllib import error, request

BASE = os.getenv("BOT_URL", "http://127.0.0.1:8080").rstrip("/")


def call(path, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"}, method="POST" if data is not None else "GET")
    try:
        with request.urlopen(req, timeout=10) as response:
            return response.status, json.load(response)
    except error.HTTPError as exc:
        return exc.code, json.load(exc)


def push(scope, cid, payload, version=1):
    return call("/v1/context", {"scope": scope, "context_id": cid, "version": version, "payload": payload, "delivered_at": "2026-04-26T10:00:00Z"})


if __name__ == "__main__":
    assert call("/v1/healthz")[0] == 200
    assert call("/v1/metadata")[1]["model"] == "deterministic-local-rules"
    assert call("/v1/teardown", {})[0] == 200
    assert call("/v1/context", raw=b"{")[0] == 400
    assert push("wrong", "x", {})[0] == 400

    categories = {}
    for name in ("dentists", "salons"):
        with open(f"dataset/categories/{name}.json", encoding="utf-8") as file:
            categories[name] = json.load(file)
        assert push("category", name, categories[name])[1]["accepted"]
    with open("dataset/merchants_seed.json", encoding="utf-8") as file:
        merchants = json.load(file)["merchants"]
    a = merchants[0]
    b = next(m for m in merchants if m["category_slug"] == "salons")
    for merchant in (a, b):
        assert push("merchant", merchant["merchant_id"], merchant)[1]["accepted"]
    assert push("merchant", a["merchant_id"], a)[0] == 200
    assert push("merchant", a["merchant_id"], a, 2)[0] == 200
    assert push("merchant", a["merchant_id"], a, 1)[0] == 409

    with open("dataset/customers_seed.json", encoding="utf-8") as file:
        customer = json.load(file)["customers"][0]
    assert push("customer", customer["customer_id"], customer)[0] == 200
    with open("dataset/triggers_seed.json", encoding="utf-8") as file:
        seed_triggers = json.load(file)["triggers"]
    triggers = [seed_triggers[0], seed_triggers[2]]
    salon_trigger = {"id": "trg_smoke_salon", "scope": "merchant", "kind": "festival_upcoming", "merchant_id": b["merchant_id"], "payload": {"festival": "Diwali", "date": "2026-10-31"}, "suppression_key": "smoke:salon", "expires_at": "2026-12-01T00:00:00Z"}
    triggers.append(salon_trigger)
    for trigger in triggers:
        assert push("trigger", trigger["id"], trigger)[0] == 200
    tick = {"now": "2026-04-26T10:30:00Z", "available_triggers": [t["id"] for t in triggers]}
    actions = call("/v1/tick", tick)[1]["actions"]
    assert len(actions) == 3, actions
    assert {a["merchant_id"] for a in actions} == {a["merchant_id"], b["merchant_id"]}
    assert next(x for x in actions if x["customer_id"])["send_as"] == "merchant_on_behalf"
    assert call("/v1/tick", tick)[1]["actions"] == []
    assert call("/v1/tick", {"now": "bad", "available_triggers": []})[0] == 400
    assert call("/v1/reply", raw=b"[")[0] == 400

    action = actions[0]
    reply = {"conversation_id": action["conversation_id"], "merchant_id": action["merchant_id"], "from_role": "merchant", "message": "Yes, send it", "received_at": "2026-04-26T10:45:00Z", "turn_number": 2}
    assert call("/v1/reply", reply)[1]["action"] == "send"
    reply["message"] = "Stop messaging me"
    assert call("/v1/reply", reply)[1]["action"] == "end"
    assert call("/v1/healthz")[1]["contexts_loaded"]["trigger"] >= 3
    print("PASS: all five endpoints, validation, versions, categories, merchants, customer, dedup, replies")
