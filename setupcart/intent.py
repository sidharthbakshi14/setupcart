"""Gemini is used ONLY for (1) parsing intent into JSON and (2) explaining an already-chosen basket.
A regex fallback keeps the demo working with no API key / no network."""
import json
import os
import re

import requests

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
LAPTOPS = {"usbc_dp", "thunderbolt", "hdmi_only"}
WORDNUM = {"one": 1, "single": 1, "two": 2, "dual": 2, "double": 2, "three": 3, "triple": 3}

PARSE_PROMPT = """Extract a desk-setup shopping request into JSON. Use null for anything not stated.
NEVER guess the laptop type. Schema:
{"use_case": "work|coding|study|gaming|content|null", "budget": int rupees or null,
 "screens": int or null, "laptop": "usbc_dp|thunderbolt|hdmi_only|null",
 "owned": {"monitor": int, "keyboard": bool, "mouse": bool, "webcam": bool, "headset": bool},
 "desk_width_cm": int or null, "small_desk": bool, "needs_webcam": bool, "needs_headset": bool}
'30k' = 30000. A bare number like 8000 is a budget in rupees. 'USB-C with DisplayPort/DP alt mode' = usbc_dp. 'only HDMI' = hdmi_only.
Request: """


def _gemini(prompt, api_key, json_mode=True):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0}}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    r = requests.post(url, params={"key": api_key}, json=body, timeout=20)
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


# ---------------------------------------------------------------- budget parsing
_NUM = r"(\d+(?:\.\d+)?)"
_UNIT = r"\s*(k|thousand|lakhs?|lacs?|l)?(?![a-z])"
_NOT_SPEC = r"(?!\s*(?:p\b|hz|cm|inch|in\b|gb|tb|mp|w\b|\"))"      # ignore 1080p, 144Hz, 24 inch ...
_UNIT_MULT = {"k": 1000, "thousand": 1000, "l": 100000, "lakh": 100000, "lakhs": 100000, "lac": 100000, "lacs": 100000}


def parse_budget(text):
    """Budget in rupees. Accepts 8000, 8,000, ₹8,000, Rs 8000, 8000 rupees, 8000/-, 8k, 1.5 lakh,
    'under 8000', 'budget is around 8000', or a reply that is just '8000'."""
    t = text.lower().replace(",", "")
    patterns = [
        r"(?:₹|rs\.?|inr|rupees?)\s*" + _NUM + _UNIT,                                  # ₹8000, rs 8000
        _NUM + _UNIT + r"\s*(?:₹|rs\b|inr\b|rupees?|/-)",                              # 8000 rupees, 8000/-
        r"\b(?:budget|under|within|below|upto|up to|max(?:imum)?|around|about|for|of|is|at|to|it|be|total|spend)\b\D{0,14}?"
        + _NUM + _UNIT + _NOT_SPEC,                                                    # under 8000
        r"^\s*" + _NUM + _UNIT + r"\s*[.!]?\s*$",                                      # reply is just "8000"
        r"\b" + _NUM + r"\s*(k|thousand|lakhs?|lacs?)(?![a-z])",                       # 30k, 1.5 lakh
    ]
    for pat in patterns:
        for m in re.finditer(pat, t):
            val = float(m.group(1)) * _UNIT_MULT.get(m.group(2) or "", 1)
            if val >= 1000:
                return int(val)
    return None


# ---------------------------------------------------------------- rule-based parser
def parse_regex(text):
    t = text.lower().replace(",", "")
    out = {"owned": {}}
    b = parse_budget(text)
    if b:
        out["budget"] = b
    m = re.search(r"(\d|one|single|two|dual|double|three|triple)\s*(?:external\s*)?(?:screens?|monitors?|displays?)", t)
    if m:
        out["screens"] = int(m.group(1)) if m.group(1).isdigit() else WORDNUM[m.group(1)]
    if "thunderbolt" in t:
        out["laptop"] = "thunderbolt"
    elif re.search(r"only hdmi|hdmi only|hdmi-only|just hdmi", t):
        out["laptop"] = "hdmi_only"
    elif re.search(r"usb-?c", t) and re.search(r"display ?port|\bdp\b|alt mode|video", t):
        out["laptop"] = "usbc_dp"
    if re.search(r"(have|own|got)\s+(a|one|an)?\s*(old\s+|existing\s+)?monitor", t):
        out["owned"]["monitor"] = 1
    for item in ("keyboard", "mouse", "webcam", "headset"):
        if re.search(rf"(have|own|got)\b[^.]*\b{item}", t):
            out["owned"][item] = True
    m = re.search(r"desk\D{0,20}(\d{2,3})\s*cm|(\d{2,3})\s*cm\s*desk", t)
    if m:
        out["desk_width_cm"] = int(m.group(1) or m.group(2))
    elif "small desk" in t:
        out["small_desk"] = True
    if re.search(r"call|meeting|zoom|teams|webcam", t) and not out["owned"].get("webcam"):
        out["needs_webcam"] = True
    if re.search(r"headset|headphone", t) and not out["owned"].get("headset"):
        out["needs_headset"] = True
    for uc in ("coding", "work", "study", "gaming", "content"):
        if uc in t:
            out["use_case"] = uc
            break
    return out


def parse(text, api_key=None):
    """Returns (partial_request, source). source is 'gemini', 'rules', or 'rules (gemini error: <code>)'."""
    if api_key:
        try:
            data = json.loads(_gemini(PARSE_PROMPT + text, api_key))
            data = {k: v for k, v in data.items() if v not in (None, {}, False)}
            if not data.get("budget"):
                b = parse_budget(text)          # safety net: Gemini missed a bare number like "8000"
                if b:
                    data["budget"] = b
            return data, "gemini"
        except Exception as e:
            # status code only: the full error text contains the API key in the URL
            code = getattr(getattr(e, "response", None), "status_code", type(e).__name__)
            return parse_regex(text), f"rules (gemini error: {code})"
    return parse_regex(text), "rules"


def merge(req, new):
    """Merge a follow-up turn into the running request, tracking stated vs inferred fields."""
    req = json.loads(json.dumps(req or {"owned": {}, "sources": {}}))
    for k, v in new.items():
        if k == "owned":
            req["owned"].update({a: b for a, b in v.items() if b})
            for a in v:
                req["sources"][f"owned.{a}"] = "stated"
        elif k == "small_desk" and v and not req.get("desk_width_cm"):
            req["desk_width_cm"] = 110
            req["sources"]["desk_width_cm"] = "inferred"
        elif k in ("budget", "screens", "laptop", "desk_width_cm", "use_case", "needs_webcam", "needs_headset"):
            if k == "laptop" and v not in LAPTOPS:
                continue
            if k in ("budget", "screens", "desk_width_cm"):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
                if v <= 0:
                    continue
            req[k] = v
            req["sources"][k] = "stated"
    req.setdefault("use_case", "work")
    if "screens" not in req:
        req["screens"] = 1
        req["sources"]["screens"] = "inferred"
    return req


def clarifications(req):
    """Clarification gate: only ask what changes whether a setup is valid."""
    qs = []
    if not req.get("budget"):
        qs.append(("budget", "What's your total budget for the setup (e.g. ₹30,000)?"))
    if not req.get("laptop"):
        qs.append(("laptop", "Which ports does your laptop have? This decides whether it can drive your screens."))
    return qs


def explain(option, req, catalog, api_key=None):
    idx = {p["id"]: p for p in catalog["products"]}
    facts = {
        "request": {k: req.get(k) for k in ("use_case", "budget", "screens", "laptop", "owned", "desk_width_cm")},
        "basket": [{"name": idx[l["id"]]["name"], "qty": l["qty"], "price": idx[l["id"]]["price"],
                    "role": idx[l["id"]]["role"]} for l in option["lines"]],
        "total": option["total"],
        "connections": [f"{d['name']} <- {o['via']} ({o['type'].upper()})" for d, o in (option["check"]["assignment"] or [])],
    }
    if api_key:
        try:
            return _gemini("Explain in 3 short bullets why this desk setup fits the request. Use ONLY these facts, "
                           "do not invent specs, prices or compatibility claims:\n" + json.dumps(facts), api_key, json_mode=False)
        except Exception:
            pass
    lines = [f"- Fits ₹{req['budget']:,} budget: total ₹{option['total']:,}." if req.get("budget") else f"- Total ₹{option['total']:,}."]
    lines.append("- Connection path: " + "; ".join(facts["connections"]) + "." if facts["connections"] else "- No video connections needed.")
    if req.get("owned"):
        owned = [k for k, v in req["owned"].items() if v]
        if owned:
            lines.append(f"- Skips items you already own: {', '.join(owned)}.")
    return "\n".join(lines)
