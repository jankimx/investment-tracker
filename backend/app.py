from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient, DESCENDING
from bson import ObjectId
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import os, json, time, urllib.request, pytz, threading
from datetime import datetime

load_dotenv()

app = Flask(__name__)
CORS(app)

# -- Config ------------------------------------------------
MONGO_URI        = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME          = os.getenv("DB_NAME", "investment_tracker")
APP_PASSWORD     = os.getenv("APP_PASSWORD", "welovetofu")
AV_KEY           = os.getenv("ALPHA_VANTAGE_KEY", "")
RESEND_KEY       = os.getenv("RESEND_API_KEY", "")
NOTIFY_VERIZON   = os.getenv("NOTIFY_VERIZON", "")
NOTIFY_ATT       = os.getenv("NOTIFY_ATT", "")
APP_URL          = os.getenv("APP_URL", "https://investment-tracker-inky.vercel.app")
FMP_KEY          = os.getenv("FMP_API_KEY", "")
CLAUDE_KEY       = os.getenv("ANTHROPIC_API_KEY", "")

# -- Database ----------------------------------------------
client   = MongoClient(MONGO_URI)
db       = client[DB_NAME]
entries  = db["entries"]
holdings = db["holdings"]
txns     = db["transactions"]
meta     = db["meta"]
analyses = db["analyses"]  # Stock analysis reports

# Create indexes once at startup
try:
    entries.create_index([("date", DESCENDING)])
    entries.create_index([("platform", 1), ("stock", 1)])
    txns.create_index([("platform", 1), ("stock", 1), ("date", DESCENDING)])
    analyses.create_index([("symbol", 1), ("analyzed_at", DESCENDING)])
except Exception as e:
    print(f"[Startup] Index warning: {e}")

# -- Helpers -----------------------------------------------
def serialize(doc):
    doc["id"] = str(doc.pop("_id"))
    return doc

def fetch_price(symbol):
    """Fetch current price and previous close from Alpha Vantage.
    Returns (price, prev_close, error)."""
    if not AV_KEY:
        return None, None, "ALPHA_VANTAGE_KEY not configured"
    try:
        url = f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={symbol}&apikey={AV_KEY}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        q = data.get("Global Quote", {})
        price = q.get("05. price")
        prev  = q.get("08. previous close")
        if price:
            return float(price), float(prev) if prev else None, None
        if "Note" in data or "Information" in data:
            return None, None, "Rate limit reached"
        return None, None, f"No data for {symbol}"
    except Exception as e:
        return None, None, str(e)

def do_refresh():
    """Fetch prices for all holdings and log entries. Returns result dict."""
    all_holdings = list(holdings.find())
    if not all_holdings:
        return {"refreshed": 0, "errors": [], "date": datetime.utcnow().strftime("%Y-%m-%d")}

    today   = datetime.utcnow().strftime("%Y-%m-%d")
    results = []
    errors  = []

    # Sort by last known value descending -- fetch all values in one query
    last_entries = {}
    for e in entries.find({}, sort=[("date", DESCENDING)]):
        k = e["platform"] + "||" + e["stock"]
        if k not in last_entries:
            last_entries[k] = e["value"]

    sorted_holdings = sorted(
        all_holdings,
        key=lambda h: last_entries.get(h["platform"] + "||" + h["stock"], 0),
        reverse=True
    )

    # Fetch one at a time with delay to respect rate limits
    # Alpha Vantage free: 5 req/min, 25 req/day
    for i, h in enumerate(sorted_holdings):
        symbol = h["stock"].upper()
        if i > 0:
            time.sleep(13)  # 13s gap = ~4.6 req/min, safely under 5/min limit

        price, prev_close, error = fetch_price(symbol)
        print(f"[Refresh] {symbol}: price={price}, prev={prev_close}, err={error}")

        if price is None:
            errors.append({"stock": symbol, "platform": h["platform"], "error": error})
            continue

        shares     = float(h["shares"])
        value      = round(price * shares, 2)
        invested   = round(float(h.get("cost_basis", 0)) * shares, 2)
        prev_value = round(prev_close * shares, 2) if prev_close else None
        daily_gain = round(value - prev_value, 2) if prev_value else None
        daily_pct  = round(daily_gain / prev_value * 100, 2) if (daily_gain is not None and prev_value) else None

        entries.update_one(
            {"date": today, "platform": h["platform"], "stock": h["stock"]},
            {"$set": {
                "date": today, "platform": h["platform"], "stock": h["stock"],
                "value": value, "invested": invested, "shares": shares,
                "price": price, "prev_close": prev_close,
                "daily_gain": daily_gain, "daily_gain_pct": daily_pct,
                "auto_logged": True, "updated_at": datetime.utcnow().isoformat()
            }},
            upsert=True
        )
        results.append({"stock": symbol, "platform": h["platform"], "value": value, "daily_gain": daily_gain})

    # Store refresh metadata
    now_utc = datetime.utcnow()
    meta.update_one(
        {"key": "last_refresh"},
        {"$set": {"key": "last_refresh", "timestamp": now_utc.isoformat(), "date": today, "count": len(results)}},
        upsert=True
    )

    # Send SMS if any succeeded
    if results:
        et_tz   = pytz.timezone("America/New_York")
        et_time = pytz.utc.localize(now_utc).astimezone(et_tz)
        send_sms(et_time.strftime("%I:%M %p").lstrip("0"))

    print(f"[Refresh] Done: {len(results)} refreshed, {len(errors)} errors")
    return {"refreshed": len(results), "results": results, "errors": errors, "date": today}

def send_sms(time_str):
    if not RESEND_KEY:
        return
    recipients = []
    if NOTIFY_VERIZON: recipients.append(f"{NOTIFY_VERIZON}@vtext.com")
    if NOTIFY_ATT:     recipients.append(f"{NOTIFY_ATT}@txt.att.net")
    if not recipients: return

    msg = f"Portfolio refreshed at {time_str} ET\nView: {APP_URL}"
    for to in recipients:
        try:
            payload = json.dumps({
                "from": "Portfolio Tracker <onboarding@resend.dev>",
                "to": [to], "subject": "", "text": msg
            }).encode()
            req = urllib.request.Request(
                "https://api.resend.com/emails", data=payload,
                headers={"Authorization": f"Bearer {RESEND_KEY}", "Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                result = json.loads(r.read().decode())
                print(f"[SMS] Sent to {to}: {result.get('id')}")
        except Exception as e:
            print(f"[SMS] Failed to {to}: {e}")

# -- Scheduler ---------------------------------------------
scheduler = BackgroundScheduler()
scheduler.add_job(
    do_refresh,
    CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone=pytz.timezone("America/New_York"))
)
scheduler.start()

# -- Auth --------------------------------------------------
@app.route("/auth", methods=["POST"])
def auth():
    data = request.get_json() or {}
    if data.get("password") == APP_PASSWORD:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Wrong password"}), 401

# -- Holdings ----------------------------------------------
@app.route("/holdings", methods=["GET"])
def get_holdings():
    return jsonify([serialize(h) for h in holdings.find()])

@app.route("/holdings", methods=["POST"])
def upsert_holding():
    d = request.get_json() or {}
    for f in ["platform", "stock", "shares"]:
        if f not in d:
            return jsonify({"error": f"Missing: {f}"}), 400
    d["shares"]     = float(d["shares"])
    d["cost_basis"] = float(d.get("cost_basis", 0))
    d["updated_at"] = datetime.utcnow().isoformat()
    holdings.update_one(
        {"platform": d["platform"], "stock": d["stock"]},
        {"$set": d}, upsert=True
    )
    return jsonify({"ok": True}), 201

@app.route("/holdings/<hid>", methods=["DELETE"])
def delete_holding(hid):
    holdings.delete_one({"_id": ObjectId(hid)})
    return jsonify({"ok": True})

# -- Transactions ------------------------------------------
@app.route("/transactions", methods=["GET"])
def get_transactions():
    q = {}
    if request.args.get("platform"): q["platform"] = request.args["platform"]
    if request.args.get("stock"):    q["stock"]    = request.args["stock"]
    return jsonify([serialize(t) for t in txns.find(q).sort("date", DESCENDING)])

@app.route("/transactions", methods=["POST"])
def add_transaction():
    d = request.get_json() or {}
    for f in ["platform", "stock", "action", "shares", "price_per_share"]:
        if f not in d:
            return jsonify({"error": f"Missing: {f}"}), 400

    platform = d["platform"]
    stock    = d["stock"].upper()
    action   = d["action"]
    shares   = float(d["shares"])
    pps      = float(d["price_per_share"])
    date     = d.get("date", datetime.utcnow().strftime("%Y-%m-%d"))

    txns.insert_one({"platform": platform, "stock": stock, "action": action,
                     "shares": shares, "price_per_share": pps, "date": date,
                     "created_at": datetime.utcnow().isoformat()})

    existing = holdings.find_one({"platform": platform, "stock": stock})
    if existing:
        old_shares = float(existing.get("shares", 0))
        old_cost   = float(existing.get("cost_basis", 0))
        if action == "buy":
            new_shares = old_shares + shares
            new_cost   = ((old_shares * old_cost) + (shares * pps)) / new_shares if new_shares > 0 else pps
        else:
            new_shares = max(0, old_shares - shares)
            new_cost   = old_cost
        holdings.update_one(
            {"platform": platform, "stock": stock},
            {"$set": {"shares": round(new_shares, 6), "cost_basis": round(new_cost, 4),
                      "updated_at": datetime.utcnow().isoformat()}}
        )
    else:
        holdings.insert_one({"platform": platform, "stock": stock,
                              "shares": shares, "cost_basis": pps,
                              "updated_at": datetime.utcnow().isoformat()})
    return jsonify({"ok": True}), 201

@app.route("/transactions/<tid>", methods=["DELETE"])
def delete_transaction(tid):
    txns.delete_one({"_id": ObjectId(tid)})
    return jsonify({"ok": True})

# -- Entries -----------------------------------------------
@app.route("/entries", methods=["GET"])
def get_entries():
    q = {}
    if request.args.get("platform"): q["platform"] = request.args["platform"]
    if request.args.get("stock"):    q["stock"]    = request.args["stock"]
    return jsonify([serialize(e) for e in entries.find(q).sort("date", DESCENDING)])

@app.route("/entries", methods=["POST"])
def add_entry():
    d = request.get_json() or {}
    for f in ["date", "platform", "stock", "value"]:
        if f not in d:
            return jsonify({"error": f"Missing: {f}"}), 400
    d["value"]      = float(d["value"])
    d["invested"]   = float(d.get("invested", 0))
    d["created_at"] = datetime.utcnow().isoformat()
    result = entries.insert_one(d)
    return jsonify({"id": str(result.inserted_id)}), 201

@app.route("/entries/<eid>", methods=["DELETE"])
def delete_entry(eid):
    entries.delete_one({"_id": ObjectId(eid)})
    return jsonify({"ok": True})

# -- Summary -----------------------------------------------
@app.route("/summary", methods=["GET"])
def get_summary():
    all_entries = list(entries.find().sort("date", DESCENDING))

    # Latest entry per platform+stock
    latest = {}
    for e in all_entries:
        k = f"{e['platform']}||{e['stock']}"
        if k not in latest:
            latest[k] = e

    total_value    = sum(e["value"] for e in latest.values())
    total_invested = sum(e.get("invested", 0) for e in latest.values())

    # Daily gain -- sum stored daily_gain from latest entries (Method 1: vs prev close)
    daily_gains = [e["daily_gain"] for e in latest.values() if e.get("daily_gain") is not None]
    daily_gain  = round(sum(daily_gains), 2) if daily_gains else None
    prev_total  = sum(e["value"] - e["daily_gain"] for e in latest.values() if e.get("daily_gain") is not None)
    daily_pct   = round(daily_gain / prev_total * 100, 2) if (daily_gain is not None and prev_total > 0) else None

    platforms = {}
    stocks    = {}
    for e in latest.values():
        platforms[e["platform"]] = round(platforms.get(e["platform"], 0) + e["value"], 2)
        stocks[e["stock"]]       = round(stocks.get(e["stock"], 0) + e["value"], 2)

    return jsonify({
        "total_value":    round(total_value, 2),
        "total_invested": round(total_invested, 2),
        "total_gain":     round(total_value - total_invested, 2),
        "daily_gain":     daily_gain,
        "daily_gain_pct": daily_pct,
        "platforms":      platforms,
        "stocks":         stocks,
        "entry_count":    len(all_entries)
    })

@app.route("/platforms", methods=["GET"])
def get_platforms():
    return jsonify(sorted(entries.distinct("platform")))

@app.route("/stocks", methods=["GET"])
def get_stocks():
    return jsonify(sorted(entries.distinct("stock")))

# -- Refresh -----------------------------------------------
@app.route("/refresh-prices", methods=["POST"])
def refresh_prices():
    return jsonify(do_refresh())

@app.route("/refresh-status", methods=["GET"])
def refresh_status():
    m = meta.find_one({"key": "last_refresh"})
    if not m:
        return jsonify({"refreshed_today": False, "last_refresh": None})
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return jsonify({
        "refreshed_today": m.get("date") == today,
        "last_refresh":    m.get("timestamp"),
        "date":            m.get("date"),
        "count":           m.get("count", 0)
    })

# -- Health ------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status":        "ok",
        "db":            DB_NAME,
        "alpha_vantage": "configured" if AV_KEY else "missing",
        "fmp":           "configured" if FMP_KEY else "missing -- add FMP_API_KEY",
        "claude":        "configured" if CLAUDE_KEY else "missing -- add ANTHROPIC_API_KEY",
        "scheduler":     "running"
    })

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
