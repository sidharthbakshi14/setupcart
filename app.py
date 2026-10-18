"""SetupCart - AI setup builder. Run: streamlit run app.py"""
import copy
import os
import uuid

import pandas as pd
import streamlit as st

import db
import engine
import events
import intent

st.set_page_config(page_title="SetupCart", page_icon="🖥️", layout="wide")
ROLE_ICON = {"monitor": "🖥️", "connection": "🔌", "cable": "〰️", "keyboard": "⌨️", "mouse": "🖱️", "webcam": "📷", "headset": "🎧"}

db.init()
ss = st.session_state
ss.catalog = db.load_catalog()          # live from SQLite on every run: stock/price changes are always current
ss.setdefault("req", None)
ss.setdefault("chat", [("assistant", "Hi! Tell me what setup you need, e.g. *“Work setup under ₹30,000, my laptop has USB-C with DisplayPort, two screens, small desk, I take calls.”*")])
ss.setdefault("result", None)
ss.setdefault("cart", [])
ss.setdefault("snapshot", {})
ss.setdefault("order", None)
ss.setdefault("blocked", [])


def idx():
    return engine.by_id(ss.catalog)


def say(text):
    ss.chat.append(("assistant", text))


# ---------------- sidebar ----------------
with st.sidebar:
    st.header("⚙️ Settings")
    api_key = st.text_input("Gemini API key (optional)", value=os.getenv("GEMINI_API_KEY", ""), type="password",
                            help="Without a key, a rule-based parser and template explanations are used.")
    st.caption(f"Mode: {'Gemini ' + intent.GEMINI_MODEL if api_key else 'Offline rules'}")
    st.caption(f"Database: `{db.DB_PATH.name}` (SQLite)")
    st.divider()
    st.header("🧪 Demo controls")
    st.caption("Catalog is **synthetic demo data**. Changes below are saved to the database.")
    labels = {p["id"]: f"{p['name']} (stock {p['stock']}, ₹{p['price']:,})" for p in ss.catalog["products"]}
    pid = st.selectbox("Product", list(labels), format_func=labels.get)
    c1, c2 = st.columns(2)
    if c1.button("Make out of stock"):
        db.update_product(pid, stock=0)
        events.log("stock_change", "observed", item=pid, stock=0)
        ss.catalog = db.load_catalog()
        st.toast(f"{idx()[pid]['name']} → out of stock")
    if c2.button("Price +10%"):
        db.update_product(pid, price=int(idx()[pid]["price"] * 1.1))
        events.log("price_change", "observed", item=pid)
        ss.catalog = db.load_catalog()
        st.toast(f"{idx()[pid]['name']} → ₹{idx()[pid]['price']:,}")
    if st.button("Reset catalog"):
        db.reset_catalog()
        ss.catalog = db.load_catalog()
    if st.button("Reset conversation"):
        for k in ("req", "result", "order"):
            ss[k] = None
        ss.cart, ss.blocked, ss.chat = [], [], ss.chat[:1]
    if st.button("Clear merchant event log"):
        events.clear()


# ---------------- core actions ----------------
def resolve():
    req = ss.req
    res = engine.recommend(req, ss.catalog)
    events.log("request_resolved", "stated", laptop=req["laptop"], budget=req.get("budget"),
               screens=req["screens"], use_case=req.get("use_case"), sources=req.get("sources", {}))
    oos = engine.oos_demand(req, ss.catalog)
    if oos:
        events.log("oos_demand", "inferred", items=oos)
    if "conflict" in res:
        events.log("no_match", "observed", constraint=res["conflict"]["constraint"], message=res["conflict"]["message"])
        say("⚠️ " + res["conflict"]["message"] + " Which constraint would you like to relax?")
    else:
        for o in res["options"]:
            o["why"] = intent.explain(o, req, ss.catalog, api_key)
        say(f"I found {len(res['options'])} valid setup(s). Every one is compatibility-checked as a whole system.")
    ss.result = res


def handle_text(text):
    ss.chat.append(("user", text))
    new, src = intent.parse(text, api_key)
    ss.req = intent.merge(ss.req, new)
    events.log("request_parsed", "stated", parser=src, fields=new)
    qs = intent.clarifications(ss.req)
    if qs:
        ss.result = None
        say(" ".join(q for _, q in qs))
    else:
        resolve()


def apply_patch(patch):
    for k, v in patch.items():
        if v is None:
            ss.req.pop(k, None)
        else:
            ss.req[k] = v
        ss.req["sources"][k] = "stated"
    events.log("constraint_relaxed", "stated", patch=patch)
    resolve()


def add_to_cart(option):
    ss.cart = copy.deepcopy(option["lines"])
    ss.snapshot = {l["id"]: idx()[l["id"]]["price"] for l in ss.cart}
    ss.order, ss.blocked = None, []
    events.log("basket_added", "observed", label=option["label"], total=option["total"], items=[l["id"] for l in ss.cart])


def checkout():
    problems = engine.recheck_for_checkout(ss.cart, ss.snapshot, ss.req, ss.catalog)
    v = engine.validate_basket(ss.cart, ss.req, ss.catalog, check_budget=False)
    hard = [m for k, m in v["issues"] if k in ("compat", "screens", "cable")]
    if problems or hard:
        ss.blocked = [m for _, _, m in problems] + hard
        events.log("checkout_blocked", "observed", reasons=ss.blocked)
        return
    total = engine.total_price(ss.cart, ss.catalog)
    order_id = "DEMO-" + uuid.uuid4().hex[:8].upper()
    race = db.place_order(order_id, ss.cart, total)       # atomic: stock decrement + order row in one transaction
    if race:
        ss.blocked = race
        events.log("checkout_blocked", "observed", reasons=race)
        return
    ss.order = {"id": order_id, "total": total}
    events.log("order_placed", "observed", order=order_id, total=total, items=[l["id"] for l in ss.cart])
    ss.cart, ss.blocked = [], []
    ss.catalog = db.load_catalog()


def render_lines(lines):
    for l in lines:
        p = idx()[l["id"]]
        st.markdown(f"{ROLE_ICON.get(p['role'], '•')} **{p['name']}** × {l['qty']} — ₹{p['price'] * l['qty']:,}")


# ---------------- UI ----------------
st.title("🖥️ SetupCart — build a setup that actually works")
shop, merchant = st.tabs(["🛒 Shopper", "📊 Merchant insights"])

with shop:
    left, right = st.columns([3, 2])
    with left:
        for role, text in ss.chat:
            st.chat_message(role).markdown(text)

        if ss.req and not ss.req.get("laptop"):
            st.caption("Pick your laptop's ports:")
            cols = st.columns(3)
            for col, (key, lap) in zip(cols, ss.catalog["laptops"].items()):
                if col.button(lap["label"], key=f"lap_{key}"):
                    ss.chat.append(("user", lap["label"]))
                    ss.req["laptop"] = key
                    ss.req["sources"]["laptop"] = "stated"
                    if intent.clarifications(ss.req):
                        say(" ".join(q for _, q in intent.clarifications(ss.req)))
                    else:
                        resolve()
                    st.rerun()

        res = ss.result
        if res and "conflict" in res:
            cols = st.columns(len(res["conflict"]["relax_options"]))
            for col, opt in zip(cols, res["conflict"]["relax_options"]):
                if col.button(opt["label"], key="relax_" + opt["label"]):
                    apply_patch(opt["patch"])
                    st.rerun()
        elif res:
            cols = st.columns(len(res["options"]))
            for i, (col, o) in enumerate(zip(cols, res["options"])):
                with col.container(border=True):
                    st.subheader(o["label"])
                    st.metric("Total", f"₹{o['total']:,}")
                    render_lines(o["lines"])
                    with st.expander("Why this setup?"):
                        st.markdown(o["why"])
                        if any(idx()[l["id"]].get("note") for l in o["lines"]):
                            st.info(" ".join(idx()[l["id"]]["note"] for l in o["lines"] if idx()[l["id"]].get("note")))
                    if st.button("Add to cart", key=f"add_{i}", type="primary" if i == 0 else "secondary"):
                        add_to_cart(o)
                        st.rerun()

        if ss.req:
            with st.expander("What I understood (stated vs inferred)"):
                st.json({k: v for k, v in ss.req.items()})

        prompt = st.chat_input("Describe your setup or answer my question…")
        if prompt:
            handle_text(prompt)
            st.rerun()

    with right:
        st.subheader("🧺 Cart")
        if ss.order:
            st.success(f"✅ Demo order **{ss.order['id']}** confirmed — ₹{ss.order['total']:,}. (No real payment taken.)")
        if not ss.cart:
            st.caption("Cart is empty.")
        else:
            for l in list(ss.cart):
                p = idx()[l["id"]]
                c1, c2 = st.columns([5, 1])
                c1.markdown(f"{ROLE_ICON.get(p['role'], '•')} {p['name']} × {l['qty']} — ₹{p['price'] * l['qty']:,}")
                if c2.button("✕", key="rm_" + l["id"]):
                    ss.cart.remove(l)
                    events.log("cart_remove", "observed", item=l["id"], role=p["role"])
                    st.rerun()
            total = engine.total_price(ss.cart, ss.catalog)
            st.metric("Cart total", f"₹{total:,}", delta=f"₹{ss.req['budget'] - total:,} left" if ss.req.get("budget") else None)

            v = engine.validate_basket(ss.cart, ss.req, ss.catalog)
            if v["ok"]:
                st.success("Setup validated: compatible, in stock, within budget.")
            else:
                for k, m in v["issues"]:
                    if k in ("compat", "screens", "cable", "stock"):
                        st.error(m)
                    else:
                        st.warning(m)
                if v["missing_cables"]:
                    if st.button("Add missing cables"):
                        for cid, q in v["missing_cables"].items():
                            ss.cart.append({"id": cid, "qty": q})
                            ss.snapshot[cid] = idx()[cid]["price"]
                        st.rerun()

            if st.button("Checkout (demo)", type="primary", key="checkout"):
                checkout()
                st.rerun()
        if ss.blocked:
            st.error("Checkout blocked by live recheck:\n\n- " + "\n- ".join(ss.blocked))
            if st.button("Find a valid replacement setup"):
                resolve()
                if ss.result and "options" in ss.result:
                    add_to_cart(ss.result["options"][0])
                    say("Stock or prices changed, so I rebuilt your setup with what's available now.")
                st.rerun()


def chart(counter):
    if counter:
        st.bar_chart(pd.Series(counter, dtype=int))
    else:
        st.caption("None yet")


with merchant:
    ev = events.load()
    ins = events.insights(ev, ss.catalog)
    st.caption("Every number below is computed from the evidence log in the database (stated / observed / inferred).")
    m = st.columns(4)
    m[0].metric("Requests resolved", ins["requests"])
    m[1].metric("No-match requests", ins["no_match"])
    m[2].metric("Orders", ins["orders"])
    m[3].metric("Checkouts blocked", ins["checkout_blocked"])
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Why requests failed (unmet needs)**")
        chart(ins["conflicts"])
        st.markdown("**Out-of-stock items customers needed** *(inferred)*")
        chart(ins["oos_demand"])
    with c2:
        st.markdown("**Items removed from recommended carts**")
        chart(ins["removed"])
        st.markdown("**Customer laptop types**")
        chart(ins["laptops"])
    orders = db.list_orders()
    with st.expander(f"Orders in database ({len(orders)})"):
        if orders:
            st.dataframe(pd.DataFrame(orders), width="stretch")
        else:
            st.caption("No orders yet.")
    st.markdown("**Roadmap (not in this demo):** demand forecasting from this event log · fraud signals at checkout")
    with st.expander(f"Raw evidence log ({len(ev)} events)"):
        st.dataframe(pd.DataFrame(ev[::-1]), width="stretch")
