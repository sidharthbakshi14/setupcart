"""Evidence event log (SQLite `events` table) -> merchant insights.
Every event is tagged stated / observed / inferred."""
import json
import time
from collections import Counter

import db


def log(event_type, evidence, **payload):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    with db.conn() as c:
        c.execute("INSERT INTO events (ts, type, evidence, payload) VALUES (?,?,?,?)",
                  (ts, event_type, evidence, json.dumps(payload, default=str)))
    return {"ts": ts, "type": event_type, "evidence": evidence, **payload}


def load():
    with db.conn() as c:
        rows = c.execute("SELECT ts, type, evidence, payload FROM events ORDER BY id").fetchall()
    return [{"ts": r["ts"], "type": r["type"], "evidence": r["evidence"], **json.loads(r["payload"])} for r in rows]


def clear():
    with db.conn() as c:
        c.execute("DELETE FROM events")


def insights(events, catalog):
    names = {p["id"]: p["name"] for p in catalog["products"]}
    requests = [e for e in events if e["type"] == "request_resolved"]
    return {
        "requests": len(requests),
        "no_match": sum(1 for e in events if e["type"] == "no_match"),
        "orders": sum(1 for e in events if e["type"] == "order_placed"),
        "checkout_blocked": sum(1 for e in events if e["type"] == "checkout_blocked"),
        "conflicts": Counter(e["constraint"] for e in events if e["type"] == "no_match"),
        "oos_demand": Counter(names.get(i, i) for e in events if e["type"] == "oos_demand" for i in e["items"]),
        "removed": Counter(names.get(e["item"], e["item"]) for e in events if e["type"] == "cart_remove"),
        "laptops": Counter(e["laptop"] for e in requests),
        "budgets": [e["budget"] for e in requests if e.get("budget")],
    }
