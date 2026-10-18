# SetupCart — AI setup builder (hackathon demo)

Tell it what setup you need → it builds a **whole basket that is guaranteed compatible**, within budget and in stock → editable cart → demo checkout with live stock/price recheck → merchant insights from an evidence log. Data lives in **SQLite**.

## Run
```bash
pip install -r requirements.txt
set GEMINI_API_KEY=your_key      # optional (macOS/Linux: export)
streamlit run app.py
python test_engine.py            # 10 tests (engine + database), uses a temp DB
```
SQLite ships with Python. `setupcart.db` is created next to `app.py` on first run and seeded from `catalog.json`.

## Files
app.py (UI) · engine.py (rules, no AI) · intent.py (Gemini + regex fallback) · db.py (SQLite) · events.py (evidence log) · catalog.json (synthetic seed data) · test_engine.py

## Database tables
products · laptops · orders · events. Checkout decrements stock and saves the order in one transaction.
**Reset catalog** reloads products from catalog.json (orders/events kept). To wipe everything, stop the app and delete `setupcart.db`.

> Catalog is **synthetic demo data**. Checkout is a demo — no payment is taken.
