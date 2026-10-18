"""Deterministic setup engine: role planning, compatibility, basket search, conflict diagnosis.
No LLM in here - every recommendation is explainable and testable."""
import itertools
from collections import Counter

VIDEO_HOST_PORTS = {"usbc_dp", "thunderbolt"}   # laptop has ONE USB-C video port in this model
PERIPHERAL_ROLES = ["keyboard", "mouse", "webcam", "headset"]
OWNED_MONITOR = {"id": "OWNED-MON", "name": "Your existing monitor", "inputs": ["hdmi"], "owned": True}


def by_id(catalog):
    return {p["id"]: p for p in catalog["products"]}


def total_price(lines, catalog):
    idx = by_id(catalog)
    return sum(idx[l["id"]]["price"] * l["qty"] for l in lines)


# ---------- 1. role planning ----------
def plan_roles(req):
    owned = req.get("owned", {})
    roles = {"monitor": max(req["screens"] - owned.get("monitor", 0), 0)}
    if not owned.get("keyboard"):
        roles["keyboard"] = 1
    if not owned.get("mouse"):
        roles["mouse"] = 1
    if req.get("needs_webcam") and not owned.get("webcam"):
        roles["webcam"] = 1
    if req.get("needs_headset") and not owned.get("headset"):
        roles["headset"] = 1
    return roles


# ---------- 2. compatibility ----------
def assign_displays(displays, conn_items, laptop):
    """Return (assignment, None) or (None, reason). assignment = [(display, output_dict)]."""
    if not displays:
        return [], None
    video_hosts = [c for c in conn_items if c["host_port"] in VIDEO_HOST_PORTS]
    if len(video_hosts) > 1:
        return None, "Only one USB-C/Thunderbolt video port is assumed; remove one dock/hub"
    outputs = [{"type": t, "via": "Laptop " + t.upper() + " port", "gpu": True} for t in laptop["native_outputs"]]
    for c in conn_items:
        if c["host_port"] not in laptop["host_ports"]:
            return None, f"{c['name']} needs a {c['host_port']} port your laptop doesn't have"
        outputs += [{"type": t, "via": c["name"], "gpu": c["uses_laptop_gpu"]} for t in c["outputs"]]
    if len(outputs) < len(displays):
        return None, f"Only {len(outputs)} video output(s) available for {len(displays)} screen(s)"
    gpu_limit_hit = False
    for perm in itertools.permutations(outputs, len(displays)):
        if sum(o["gpu"] for o in perm) > laptop["max_external_displays"]:
            gpu_limit_hit = True
            continue
        if all(o["type"] in d["inputs"] for d, o in zip(displays, perm)):
            return list(zip(displays, perm)), None
    if gpu_limit_hit:
        return None, f"Laptop drives at most {laptop['max_external_displays']} external display(s) natively"
    return None, "No output matches the monitor inputs"


def required_cables(assignment):
    need = Counter()
    for d, o in assignment:
        if d.get("owned"):
            continue  # inferred: customer already has a cable for their own monitor
        if d.get("included_cable") != o["type"]:
            need["CBL-" + o["type"].upper()] += 1
    return need


# ---------- 3. full-basket validation (used by search AND cart edits) ----------
def validate_basket(lines, req, catalog, check_budget=True, check_stock=True, check_desk=True):
    idx = by_id(catalog)
    laptop = catalog["laptops"][req["laptop"]]
    issues, missing_cables = [], Counter()
    items = [(idx[l["id"]], l["qty"]) for l in lines]

    if check_stock:
        for p, q in items:
            if p["stock"] < q:
                issues.append(("stock", f"{p['name']}: only {p['stock']} in stock"))

    displays = [p for p, q in items if p["role"] == "monitor" for _ in range(q)]
    displays += [OWNED_MONITOR] * req.get("owned", {}).get("monitor", 0)
    if len(displays) != req["screens"]:
        issues.append(("screens", f"Basket gives {len(displays)} screen(s); you asked for {req['screens']}"))

    conn = [p for p, q in items if p["role"] == "connection" for _ in range(q)]
    assignment, reason = assign_displays(displays, conn, laptop)
    if reason:
        issues.append(("compat", reason))
    else:
        have = Counter({p["id"]: q for p, q in items if p["role"] == "cable"})
        missing_cables = required_cables(assignment) - have
        for cid, q in missing_cables.items():
            issues.append(("cable", f"Missing {q} x {idx[cid]['name']}"))

    if check_desk and req.get("desk_width_cm"):
        width = sum(d.get("width_cm", 0) for d in displays if not d.get("owned"))
        width += 55 * req.get("owned", {}).get("monitor", 0)  # inferred width for owned monitor
        if width > req["desk_width_cm"]:
            issues.append(("desk", f"Screens need ~{width} cm; desk is {req['desk_width_cm']} cm"))

    if check_budget and req.get("budget"):
        t = total_price(lines, catalog)
        if t > req["budget"]:
            issues.append(("budget", f"Total ₹{t:,} exceeds budget ₹{req['budget']:,}"))
    return {"ok": not issues, "issues": issues, "assignment": assignment, "missing_cables": missing_cables}


# ---------- 4. basket search ----------
def _score(lines, catalog):
    idx = by_id(catalog)
    w = {"monitor": 2.0, "connection": 1.0, "cable": 0.0}
    return sum(idx[l["id"]]["rating"] * w.get(idx[l["id"]]["role"], 1.0) * l["qty"] for l in lines)


def _lines(ids):
    return [{"id": i, "qty": q} for i, q in Counter(ids).items()]


def _cores(req, catalog, ignore_stock=False, ignore_desk=False):
    """All valid display cores (monitors + connection + auto-added cables)."""
    roles = plan_roles(req)
    prods = [p for p in catalog["products"] if ignore_stock or p["stock"] > 0]
    mons = [p for p in prods if p["role"] == "monitor"]
    conns = [p for p in prods if p["role"] == "connection"]
    conn_sets = [()] + [(c,) for c in conns] + list(itertools.combinations(conns, 2))
    laptop = catalog["laptops"][req["laptop"]]
    cores = []
    for mset in itertools.combinations_with_replacement(mons, roles["monitor"]):
        for cset in conn_sets:
            ids = [m["id"] for m in mset] + [c["id"] for c in cset]
            v = validate_basket(_lines(ids), req, catalog, check_budget=False,
                                check_stock=not ignore_stock, check_desk=not ignore_desk)
            if any(k in ("compat", "screens", "desk", "stock") for k, _ in v["issues"]):
                continue
            # prune redundant connection devices (don't sell a dock nobody needs)
            displays = list(mset) + [OWNED_MONITOR] * req.get("owned", {}).get("monitor", 0)
            if any(assign_displays(displays, [x for x in cset if x is not c], laptop)[1] is None for c in cset):
                continue
            ids += list(v["missing_cables"].elements())  # auto-add required cables
            cores.append(ids)
    return cores


def _peripheral_sets(req, catalog, ignore_stock=False):
    roles = plan_roles(req)
    pools = []
    for r in PERIPHERAL_ROLES:
        if roles.get(r):
            pools.append([p["id"] for p in catalog["products"]
                          if p["role"] == r and (ignore_stock or p["stock"] > 0)])
    return list(itertools.product(*pools)) if pools else [()]


def search(req, catalog, budget=None, ignore_stock=False, ignore_desk=False):
    budget = req.get("budget") if budget is None else budget
    cores = _cores(req, catalog, ignore_stock, ignore_desk)
    peris = _peripheral_sets(req, catalog, ignore_stock)
    baskets = []
    for core in cores:
        for ps in peris:
            lines = _lines(core + list(ps))
            baskets.append((lines, total_price(lines, catalog), _score(lines, catalog)))
    within = [b for b in baskets if budget is None or b[1] <= budget]
    return baskets, within


def recommend(req, catalog):
    """Returns {'options': [...]} or {'conflict': {...}}."""
    all_b, within = search(req, catalog)
    if within:
        best = max(within, key=lambda b: (b[2], -b[1]))
        cheapest = min(within, key=lambda b: (b[1], -b[2]))
        options = [{"label": "Recommended", "lines": best[0], "total": best[1]}]
        if cheapest[0] != best[0]:
            options.append({"label": "Cheaper (with compromises)", "lines": cheapest[0], "total": cheapest[1]})
        cap = int(req["budget"] * 1.25) if req.get("budget") else None
        over = [b for b in all_b if cap and req["budget"] < b[1] <= cap and b[2] > best[2]]
        if over:
            up = max(over, key=lambda b: (b[2], -b[1]))
            options.append({"label": f"Upgrade (+₹{up[1] - req['budget']:,} over budget)", "lines": up[0], "total": up[1]})
        for o in options:
            o["check"] = validate_basket(o["lines"], req, catalog, check_budget=False)
        return {"options": options}
    return {"conflict": diagnose(req, catalog, all_b)}


# ---------- 5. honest no-match ----------
def diagnose(req, catalog, all_b=None):
    if all_b is None:
        all_b, _ = search(req, catalog)
    if all_b:
        cheapest = min(b[1] for b in all_b)
        relax = [{"label": f"Raise budget to ₹{cheapest:,}", "patch": {"budget": cheapest}}]
        if req["screens"] > 1:
            fewer = dict(req, screens=req["screens"] - 1)
            _, w = search(fewer, catalog)
            if w:
                relax.append({"label": f"Go to {fewer['screens']} screen(s) (from ₹{min(b[1] for b in w):,})",
                              "patch": {"screens": fewer["screens"]}})
        if req.get("needs_webcam") or req.get("needs_headset"):
            lean = dict(req, needs_webcam=False, needs_headset=False)
            _, w = search(lean, catalog)
            if w:
                relax.append({"label": f"Skip webcam/headset (from ₹{min(b[1] for b in w):,})",
                              "patch": {"needs_webcam": False, "needs_headset": False}})
        return {"constraint": "budget",
                "message": f"The cheapest valid setup costs ₹{cheapest:,}, ₹{cheapest - req['budget']:,} over your ₹{req['budget']:,} budget.",
                "relax_options": relax}
    if search(req, catalog, budget=10**9, ignore_desk=True)[0]:
        return {"constraint": "desk", "message": f"No in-stock screen combination fits a {req['desk_width_cm']} cm desk.",
                "relax_options": [{"label": "Ignore desk width", "patch": {"desk_width_cm": None}},
                                  {"label": "Go to fewer screens", "patch": {"screens": max(req["screens"] - 1, 1)}}]}
    if search(req, catalog, budget=10**9, ignore_stock=True)[0]:
        return {"constraint": "stock", "message": "A valid setup exists, but a required item is out of stock.",
                "relax_options": [{"label": "Go to fewer screens", "patch": {"screens": max(req["screens"] - 1, 1)}}]}
    laptop = catalog["laptops"][req["laptop"]]
    return {"constraint": "compatibility",
            "message": f"A '{laptop['label']}' laptop can't drive {req['screens']} screens with any item we stock.",
            "relax_options": [{"label": f"Go to {laptop['max_external_displays']} screen(s)",
                               "patch": {"screens": laptop["max_external_displays"]}}]}


def oos_demand(req, catalog):
    """Out-of-stock items that would have been in the recommended basket (merchant signal)."""
    _, within = search(req, catalog, ignore_stock=True)
    if not within:
        return []
    best = max(within, key=lambda b: (b[2], -b[1]))[0]
    idx = by_id(catalog)
    return [l["id"] for l in best if idx[l["id"]]["stock"] < l["qty"]]


def recheck_for_checkout(cart_lines, price_snapshot, req, catalog):
    """Live stock + price recheck right before order placement."""
    idx = by_id(catalog)
    problems = []
    for l in cart_lines:
        p = idx[l["id"]]
        if p["stock"] < l["qty"]:
            problems.append(("stock", l["id"], f"{p['name']} is now out of stock"))
        if p["price"] != price_snapshot.get(l["id"], p["price"]):
            problems.append(("price", l["id"], f"{p['name']} price changed ₹{price_snapshot[l['id']]:,} → ₹{p['price']:,}"))
    return problems
