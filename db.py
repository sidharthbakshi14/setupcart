"""SQLite persistence: catalog (laptops, products), orders, evidence events.
The file setupcart.db is created next to app.py and seeded from catalog.json on first run.
Override the location with SETUPCART_DB=/path/to/file.db"""
import json
import os
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(os.getenv("SETUPCART_DB", Path(__file__).with_name("setupcart.db")))
SEED_PATH = Path(__file__).with_name("catalog.json")
CORE = ("id", "role", "name", "price", "stock", "rating")

SCHEMA = """
CREATE TABLE IF NOT EXISTS laptops (
    key TEXT PRIMARY KEY, label TEXT NOT NULL, native_outputs TEXT NOT NULL,
    host_ports TEXT NOT NULL, max_external_displays INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS products (
    id TEXT PRIMARY KEY, role TEXT NOT NULL, name TEXT NOT NULL,
    price INTEGER NOT NULL CHECK (price >= 0), stock INTEGER NOT NULL CHECK (stock >= 0),
    rating REAL, attrs TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, ts TEXT NOT NULL, total INTEGER NOT NULL, items TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, type TEXT NOT NULL,
    evidence TEXT NOT NULL CHECK (evidence IN ('stated','observed','inferred')),
    payload TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
"""


def conn():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _seed(c):
    data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    for key, l in data["laptops"].items():
        c.execute("INSERT INTO laptops VALUES (?,?,?,?,?)",
                  (key, l["label"], json.dumps(l["native_outputs"]), json.dumps(l["host_ports"]),
                   l["max_external_displays"]))
    for p in data["products"]:
        attrs = {k: v for k, v in p.items() if k not in CORE}
        c.execute("INSERT INTO products VALUES (?,?,?,?,?,?,?)",
                  (p["id"], p["role"], p["name"], p["price"], p["stock"], p.get("rating"), json.dumps(attrs)))


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        if c.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0:
            _seed(c)


def load_catalog():
    """Same dict shape engine.py already uses: {'laptops': {...}, 'products': [...]}."""
    with conn() as c:
        laptops = {r["key"]: {"label": r["label"], "native_outputs": json.loads(r["native_outputs"]),
                              "host_ports": json.loads(r["host_ports"]),
                              "max_external_displays": r["max_external_displays"]}
                   for r in c.execute("SELECT * FROM laptops ORDER BY rowid")}
        products = []
        for r in c.execute("SELECT * FROM products ORDER BY rowid"):
            p = {k: r[k] for k in CORE}
            p.update(json.loads(r["attrs"]))
            products.append(p)
    return {"laptops": laptops, "products": products}


def update_product(pid, stock=None, price=None):
    with conn() as c:
        if stock is not None:
            c.execute("UPDATE products SET stock=? WHERE id=?", (max(int(stock), 0), pid))
        if price is not None:
            c.execute("UPDATE products SET price=? WHERE id=?", (max(int(price), 0), pid))


def reset_catalog():
    """Restore products/laptops from catalog.json. Orders and events are kept."""
    with conn() as c:
        c.execute("DELETE FROM products")
        c.execute("DELETE FROM laptops")
        _seed(c)


def place_order(order_id, lines, total):
    """Atomically decrement stock and save the order. Returns [] on success, else a list of problems.
    If any line lacks stock, nothing is changed."""
    problems = []
    c = conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        for l in lines:
            cur = c.execute("UPDATE products SET stock = stock - ? WHERE id = ? AND stock >= ?",
                            (l["qty"], l["id"], l["qty"]))
            if cur.rowcount != 1:
                problems.append(f"{l['id']} no longer has {l['qty']} in stock")
        if problems:
            c.rollback()
            return problems
        c.execute("INSERT INTO orders VALUES (?,?,?,?)",
                  (order_id, time.strftime("%Y-%m-%d %H:%M:%S"), total, json.dumps(lines)))
        c.commit()
        return []
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def list_orders():
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT id, ts, total, items FROM orders ORDER BY ts DESC")]
