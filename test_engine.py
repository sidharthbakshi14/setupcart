"""Run: python test_engine.py   (or: python -m pytest -q)"""
import copy
import json
import os
import tempfile
from pathlib import Path

os.environ["SETUPCART_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")  # tests never touch your real DB

import db  # noqa: E402
import engine  # noqa: E402
import events  # noqa: E402
import intent  # noqa: E402

CAT = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))


def req_from(text):
    return intent.merge(None, intent.parse_regex(text))


def ids(option):
    return {l["id"]: l["qty"] for l in option["lines"]}


def test_1_happy_path_usbc_two_screens():
    r = req_from("Work setup under ₹30,000, my laptop has USB-C with DisplayPort, two screens, small desk")
    assert r["budget"] == 30000 and r["screens"] == 2 and r["laptop"] == "usbc_dp"
    best = engine.recommend(r, CAT)["options"][0]
    assert best["total"] <= 30000
    assert best["check"]["ok"], best["check"]["issues"]
    assert sum(q for i, q in ids(best).items() if i.startswith("MON")) == 2


def test_2_owned_monitor_not_rebought():
    r = req_from("I already have a monitor. Need two screens, budget 25k, thunderbolt laptop")
    best = engine.recommend(r, CAT)["options"][0]
    assert sum(q for i, q in ids(best).items() if i.startswith("MON")) == 1
    assert best["check"]["ok"]


def test_3_impossible_budget_explains_conflict():
    r = req_from("₹8000 for two monitors, USB-C with DP laptop")
    res = engine.recommend(r, CAT)
    assert "conflict" in res and res["conflict"]["constraint"] == "budget"
    assert res["conflict"]["relax_options"]


def test_4_stock_change_blocks_checkout():
    r = req_from("Work setup ₹30k, USB-C with DisplayPort, two screens")
    best = engine.recommend(r, CAT)["options"][0]
    snapshot = {l["id"]: engine.by_id(CAT)[l["id"]]["price"] for l in best["lines"]}
    live = copy.deepcopy(CAT)
    mon_id = next(l["id"] for l in best["lines"] if l["id"].startswith("MON"))
    engine.by_id(live)[mon_id]["stock"] = 0
    problems = engine.recheck_for_checkout(best["lines"], snapshot, r, live)
    assert problems and problems[0][0] == "stock"
    alt = engine.recommend(r, live)
    assert mon_id not in ids(alt["options"][0])


def test_5_hdmi_only_needs_displaylink_for_two_screens():
    r = req_from("hdmi only laptop, two monitors, budget 40k")
    best = engine.recommend(r, CAT)["options"][0]
    assert "ADPT-DL" in ids(best)


def test_6_cart_edit_removing_dock_is_flagged():
    r = req_from("Work setup ₹30k, USB-C with DisplayPort, two screens")
    best = engine.recommend(r, CAT)["options"][0]
    edited = [l for l in best["lines"] if engine.by_id(CAT)[l["id"]]["role"] != "connection"]
    v = engine.validate_basket(edited, r, CAT)
    assert any(k == "compat" for k, _ in v["issues"])


def test_7_db_seed_roundtrip_matches_json():
    db.init()
    loaded = db.load_catalog()
    assert [p["id"] for p in loaded["products"]] == [p["id"] for p in CAT["products"]]
    assert loaded["laptops"] == CAT["laptops"]
    dock = next(p for p in loaded["products"] if p["id"] == "DOCK-2H")
    assert dock["outputs"] == ["hdmi", "hdmi"] and dock["uses_laptop_gpu"] is True
    r = req_from("Work setup ₹30k, USB-C with DisplayPort, two screens")
    assert engine.recommend(r, loaded)["options"][0]["total"] == engine.recommend(r, CAT)["options"][0]["total"]


def test_8_db_stock_persists_and_order_is_atomic():
    db.init()
    db.reset_catalog()
    db.update_product("MON-215A", stock=1)
    assert next(p for p in db.load_catalog()["products"] if p["id"] == "MON-215A")["stock"] == 1
    bad = [{"id": "KB-W", "qty": 1}, {"id": "MON-215A", "qty": 2}]
    before = {p["id"]: p["stock"] for p in db.load_catalog()["products"]}
    assert db.place_order("T-BAD", bad, 1) != []
    assert {p["id"]: p["stock"] for p in db.load_catalog()["products"]} == before
    assert db.list_orders() == []
    ok = [{"id": "KB-W", "qty": 2}, {"id": "MON-215A", "qty": 1}]
    assert db.place_order("T-OK", ok, 100) == []
    after = {p["id"]: p["stock"] for p in db.load_catalog()["products"]}
    assert after["KB-W"] == before["KB-W"] - 2 and after["MON-215A"] == 0
    assert [o["id"] for o in db.list_orders()] == ["T-OK"]


def test_9_events_roundtrip_and_clear():
    db.init()
    events.clear()
    events.log("no_match", "observed", constraint="budget", message="x")
    events.log("request_resolved", "stated", laptop="usbc_dp", budget=30000, screens=2)
    ev = events.load()
    assert [e["type"] for e in ev] == ["no_match", "request_resolved"]
    ins = events.insights(ev, CAT)
    assert ins["no_match"] == 1 and ins["conflicts"]["budget"] == 1 and ins["laptops"]["usbc_dp"] == 1
    events.clear()
    assert events.load() == []


def test_10_budget_without_rupee_symbol():
    for t in ("8000", "8,000", "₹8000", "Rs 8000", "8k", "under 8000", "budget is 8000"):
        assert intent.parse_budget(t) == 8000, t
    assert intent.parse_budget("1080p webcam 144Hz monitor") is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
