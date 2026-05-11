from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient, DESCENDING
from bson import ObjectId
from dotenv import load_dotenv
import os
import urllib.request
import json
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

load_dotenv()

app = Flask(__name__)
CORS(app)

@app.before_request
def setup_indexes():
    global _indexes_created
    if not _indexes_created:
        ensure_indexes()
        _indexes_created = True

_indexes_created = False

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME = os.getenv("DB_NAME", "investment_tracker")
ALPHA_VANTAGE_KEY = os.getenv("ALPHA_VANTAGE_KEY", "")
APP_PASSWORD = os.getenv("APP_PASSWORD", "welovetofu")

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
entries_col = db["entries"]
holdings_col = db["holdings"]

def ensure_indexes():
    try:
        entries_col.create_index([("date", DESCENDING)])
        entries_col.create_index([("platform", 1)])
        entries_col.create_index([("stock", 1)])
    except Exception as e:
        print("Index creation warning:", e)


def serialize(doc):
    doc["id"] = str(doc["_id"])
    del doc["_id"]
    return doc


def fetch_price(symbol):
    if not ALPHA_VANTAGE_KEY:
        return None, "No API key configured"
    try:
        url = "https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol=" + symbol + "&apikey=" + ALPHA_VANTAGE_KEY
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
        quote = data.get("Global Quote", {})
        price = quote.get("05. price")
        if price:
            return float(price), None
        if "Note" in data or "Information" in data:
            return None, "API rate limit reached"
        return None, "No price data for " + symbol
    except Exception as e:
        return None, str(e)


def do_refresh_prices():
    """Core price refresh logic — used by both the API endpoint and the scheduler."""
    holdings = list(holdings_col.find())
    if not holdings:
        return {"refreshed": 0, "results": [], "errors": [], "date": datetime.utcnow().strftime("%Y-%m-%d")}

    today = datetime.utcnow().strftime("%Y-%m-%d")
    results = []
    errors = []

    BATCH_SIZE = 5
    BATCH_WAIT = 13

    def process_holding(h):
        symbol = h["stock"].upper()
        price, error = fetch_price(symbol)
        return h, symbol, price, error

    batches = [holdings[i:i+BATCH_SIZE] for i in range(0, len(holdings), BATCH_SIZE)]

    for batch_num, batch in enumerate(batches):
        if batch_num > 0:
            time.sleep(BATCH_WAIT)

        with ThreadPoolExecutor(max_workers=BATCH_SIZE) as executor:
            futures = {executor.submit(process_holding, h): h for h in batch}
            for future in as_completed(futures):
                h, symbol, price, error = future.result()
                if price is None:
                    errors.append({"stock": symbol, "platform": h["platform"], "error": error})
                    continue

                shares = h["shares"]
                value = round(price * shares, 2)
                invested = round(h.get("cost_basis", 0) * shares, 2)

                entries_col.update_one(
                    {"date": today, "platform": h["platform"], "stock": h["stock"]},
                    {"$set": {
                        "date": today,
                        "platform": h["platform"],
                        "stock": h["stock"],
                        "value": value,
                        "invested": invested,
                        "shares": shares,
                        "price": price,
                        "auto_logged": True,
                        "updated_at": datetime.utcnow().isoformat()
                    }},
                    upsert=True
                )
                results.append({
                    "stock": symbol,
                    "platform": h["platform"],
                    "shares": shares,
                    "price": price,
                    "value": value
                })

    return {"refreshed": len(results), "results": results, "errors": errors, "date": today}


def scheduled_refresh():
    """Called automatically by the scheduler."""
    print("[Scheduler] Auto-refreshing prices at " + datetime.utcnow().isoformat())
    result = do_refresh_prices()
    print("[Scheduler] Done — refreshed " + str(result["refreshed"]) + " holdings, " + str(len(result["errors"])) + " errors")


# ── Scheduler — runs at 4pm ET (21:00 UTC) daily on weekdays ──
scheduler = BackgroundScheduler()
et = pytz.timezone("America/New_York")
scheduler.add_job(
    scheduled_refresh,
    CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone=et)
)
scheduler.start()


# ── Auth ──────────────────────────────────────────────────

@app.route("/auth", methods=["POST"])
def auth():
    data = request.json or {}
    if data.get("password") == APP_PASSWORD:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Wrong password"}), 401


# ── Holdings ──────────────────────────────────────────────

@app.route("/holdings", methods=["GET"])
def get_holdings():
    holdings = [serialize(h) for h in holdings_col.find()]
    return jsonify(holdings)


@app.route("/holdings", methods=["POST"])
def upsert_holding():
    data = request.json
    for field in ["platform", "stock", "shares"]:
        if field not in data:
            return jsonify({"error": "Missing field: " + field}), 400
    data["shares"] = float(data["shares"])
    data["cost_basis"] = float(data.get("cost_basis", 0))
    data["updated_at"] = datetime.utcnow().isoformat()
    holdings_col.update_one(
        {"platform": data["platform"], "stock": data["stock"]},
        {"$set": data},
        upsert=True
    )
    return jsonify({"message": "Holding saved"}), 201


@app.route("/holdings/<holding_id>", methods=["DELETE"])
def delete_holding(holding_id):
    holdings_col.delete_one({"_id": ObjectId(holding_id)})
    return jsonify({"deleted": holding_id})


@app.route("/refresh-prices", methods=["POST"])
def refresh_prices():
    result = do_refresh_prices()
    return jsonify(result)


# ── Entries ───────────────────────────────────────────────

@app.route("/entries", methods=["GET"])
def get_entries():
    query = {}
    if request.args.get("platform"):
        query["platform"] = request.args.get("platform")
    if request.args.get("stock"):
        query["stock"] = request.args.get("stock")
    result = [serialize(e) for e in entries_col.find(query).sort("date", DESCENDING)]
    return jsonify(result)


@app.route("/entries", methods=["POST"])
def add_entry():
    data = request.json
    for field in ["date", "platform", "stock", "value"]:
        if field not in data:
            return jsonify({"error": "Missing field: " + field}), 400
    data["value"] = float(data["value"])
    data["invested"] = float(data.get("invested", 0))
    data["created_at"] = datetime.utcnow().isoformat()
    result = entries_col.insert_one(data)
    return jsonify({"id": str(result.inserted_id), "message": "Entry saved"}), 201


@app.route("/entries/<entry_id>", methods=["PUT"])
def update_entry(entry_id):
    data = request.json
    if "value" in data:
        data["value"] = float(data["value"])
    if "invested" in data:
        data["invested"] = float(data["invested"])
    data["updated_at"] = datetime.utcnow().isoformat()
    entries_col.update_one({"_id": ObjectId(entry_id)}, {"$set": data})
    return jsonify({"message": "Updated"})


@app.route("/entries/<entry_id>", methods=["DELETE"])
def delete_entry(entry_id):
    entries_col.delete_one({"_id": ObjectId(entry_id)})
    return jsonify({"deleted": entry_id})


# ── Summary ───────────────────────────────────────────────

@app.route("/summary", methods=["GET"])
def get_summary():
    all_entries = list(entries_col.find().sort("date", DESCENDING))
    latest = {}
    for e in all_entries:
        key = e["platform"] + "||" + e["stock"]
        if key not in latest:
            latest[key] = e

    total_value = sum(e["value"] for e in latest.values())
    total_invested = sum(e.get("invested", 0) for e in latest.values())

    platforms = {}
    for e in latest.values():
        pf = e["platform"]
        platforms[pf] = platforms.get(pf, 0) + e["value"]

    stocks = {}
    for e in latest.values():
        st = e["stock"]
        stocks[st] = stocks.get(st, 0) + e["value"]

    return jsonify({
        "total_value": round(total_value, 2),
        "total_invested": round(total_invested, 2),
        "total_gain": round(total_value - total_invested, 2),
        "platforms": platforms,
        "stocks": stocks,
        "entry_count": len(all_entries)
    })


@app.route("/platforms", methods=["GET"])
def get_platforms():
    return jsonify(sorted(entries_col.distinct("platform")))


@app.route("/stocks", methods=["GET"])
def get_stocks():
    return jsonify(sorted(entries_col.distinct("stock")))


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "db": DB_NAME,
        "alpha_vantage": "configured" if ALPHA_VANTAGE_KEY else "missing",
        "scheduler": "running",
        "next_refresh": "weekdays at 4:00pm ET"
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
