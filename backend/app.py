from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient, DESCENDING, ASCENDING
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from functools import wraps
import os, json, time, secrets, urllib.request, pytz, threading
from datetime import datetime, timedelta

load_dotenv()

app = Flask(__name__)
CORS(app, expose_headers=["Authorization"])

# -- Config ------------------------------------------------
MONGO_URI        = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME          = os.getenv("DB_NAME", "investment_tracker")
APP_PASSWORD     = os.getenv("APP_PASSWORD")  # required, no default
AV_KEY           = os.getenv("ALPHA_VANTAGE_KEY", "")
RESEND_KEY       = os.getenv("RESEND_API_KEY", "")
NOTIFY_VERIZON   = os.getenv("NOTIFY_VERIZON", "")
NOTIFY_ATT       = os.getenv("NOTIFY_ATT", "")
APP_URL          = os.getenv("APP_URL", "https://investment-tracker-inky.vercel.app")
FMP_KEY          = os.getenv("FMP_API_KEY", "")
CLAUDE_KEY       = os.getenv("ANTHROPIC_API_KEY", "")

if not APP_PASSWORD:
    raise RuntimeError("APP_PASSWORD env var must be set -- refusing to start with no password.")

SESSION_TTL_DAYS = 30
AV_DAILY_LIMIT   = 25  # Alpha Vantage free tier

# -- Database ----------------------------------------------
client   = MongoClient(MONGO_URI)
db       = client[DB_NAME]
entries  = db["entries"]
holdings = db["holdings"]
txns     = db["transactions"]
meta     = db["meta"]
analyses = db["analyses"]  # Stock analysis reports
sessions = db["sessions"]  # Auth tokens

# Create indexes once at startup
try:
    entries.create_index([("date", DESCENDING)])
    entries.create_index([("platform", 1), ("stock", 1)])
    txns.create_index([("platform", 1), ("stock", 1), ("date", DESCENDING)])
    analyses.create_index([("symbol", 1), ("analyzed_at", DESCENDING)])
    sessions.create_index([("token", ASCENDING)], unique=True)
    # TTL index — Mongo deletes session docs once expires_at is in the past
    sessions.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
except Exception as e:
    print(f"[Startup] Index warning: {e}")

# -- Helpers -----------------------------------------------
def serialize(doc):
    doc["id"] = str(doc.pop("_id"))
    return doc

def safe_object_id(value):
    """Parse an ObjectId from user input, returning None if invalid."""
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None

# -- Auth ---------------------------------------------------
def issue_token():
    """Create a fresh session token and store it. Returns the token string."""
    token = secrets.token_urlsafe(32)
    now   = datetime.utcnow()
    sessions.insert_one({
        "token":      token,
        "created_at": now,
        "expires_at": now + timedelta(days=SESSION_TTL_DAYS),
    })
    return token

def revoke_token(token):
    if token:
        sessions.delete_one({"token": token})

def _bearer_token():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return None

def require_auth(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        token = _bearer_token()
        if not token:
            return jsonify({"error": "Missing token"}), 401
        s = sessions.find_one({"token": token})
        if not s or s.get("expires_at", datetime.min) < datetime.utcnow():
            return jsonify({"error": "Invalid or expired token"}), 401
        return fn(*args, **kwargs)
    return wrapper

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

    # Sort by last known value descending so the most valuable positions are
    # refreshed first if we hit the API daily cap. One aggregation, not a full scan.
    last_value = {
        e["platform"] + "||" + e["stock"]: e.get("value", 0)
        for e in latest_entries_per_position()
    }
    sorted_holdings = sorted(
        all_holdings,
        key=lambda h: last_value.get(h["platform"] + "||" + h["stock"], 0),
        reverse=True
    )

    # Cap at AV's daily limit. Anything past the cap is reported as an error
    # rather than silently swallowed by Alpha Vantage's rate-limit response.
    if len(sorted_holdings) > AV_DAILY_LIMIT:
        for h in sorted_holdings[AV_DAILY_LIMIT:]:
            errors.append({"stock": h["stock"], "platform": h["platform"],
                           "error": f"Skipped: exceeds Alpha Vantage daily limit ({AV_DAILY_LIMIT})"})
        sorted_holdings = sorted_holdings[:AV_DAILY_LIMIT]

    # Fetch one at a time with delay to respect AV's 5 req/min limit
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
# Each gunicorn worker imports this module, so each worker spins up its own
# scheduler. To avoid N duplicate refreshes (and N SMS texts), we use a Mongo
# unique-key tick: the first worker to insert {_id: "tick_<timestamp>"} runs
# the job; the rest hit DuplicateKeyError and bail.
def _scheduled_refresh():
    et_now = datetime.now(pytz.timezone("America/New_York"))
    tick_id = "refresh_tick_" + et_now.strftime("%Y-%m-%dT%H:%M")
    try:
        meta.insert_one({
            "_id": tick_id,
            "fired_at": datetime.utcnow().isoformat(),
            "pid": os.getpid(),
        })
    except DuplicateKeyError:
        print(f"[Scheduler] Tick {tick_id} already claimed by another worker; skipping")
        return
    print(f"[Scheduler] Worker pid={os.getpid()} won tick {tick_id}, running refresh")
    _run_refresh_async()

scheduler = BackgroundScheduler()
scheduler.add_job(
    _scheduled_refresh,
    CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone=pytz.timezone("America/New_York")),
    coalesce=True, max_instances=1,
)
scheduler.start()

# -- Auth routes -------------------------------------------
@app.route("/auth", methods=["POST"])
def auth():
    data = request.get_json() or {}
    # constant-time compare to avoid timing oracle on the password
    if not secrets.compare_digest(str(data.get("password", "")), APP_PASSWORD):
        return jsonify({"ok": False, "error": "Wrong password"}), 401
    token = issue_token()
    return jsonify({"ok": True, "token": token, "ttl_days": SESSION_TTL_DAYS})

@app.route("/auth/logout", methods=["POST"])
def logout():
    revoke_token(_bearer_token())
    return jsonify({"ok": True})

# -- Latest-entry aggregation helper -----------------------
def latest_entries_per_position(extra_match=None):
    """One Mongo round-trip to fetch the latest entry per (platform, stock).
    Returns a list of entry dicts (without _id mapping)."""
    pipeline = []
    if extra_match:
        pipeline.append({"$match": extra_match})
    pipeline += [
        {"$sort": {"date": DESCENDING}},
        {"$group": {
            "_id":    {"platform": "$platform", "stock": "$stock"},
            "latest": {"$first": "$$ROOT"},
        }},
    ]
    return [r["latest"] for r in entries.aggregate(pipeline)]

# -- Holdings ----------------------------------------------
@app.route("/holdings", methods=["GET"])
@require_auth
def get_holdings():
    return jsonify([serialize(h) for h in holdings.find()])

@app.route("/holdings", methods=["POST"])
@require_auth
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
@require_auth
def delete_holding(hid):
    oid = safe_object_id(hid)
    if oid is None:
        return jsonify({"error": "Invalid id"}), 400
    holdings.delete_one({"_id": oid})
    return jsonify({"ok": True})

# -- Transactions ------------------------------------------
@app.route("/transactions", methods=["GET"])
@require_auth
def get_transactions():
    q = {}
    if request.args.get("platform"): q["platform"] = request.args["platform"]
    if request.args.get("stock"):    q["stock"]    = request.args["stock"]
    return jsonify([serialize(t) for t in txns.find(q).sort("date", DESCENDING)])

@app.route("/transactions", methods=["POST"])
@require_auth
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
@require_auth
def delete_transaction(tid):
    oid = safe_object_id(tid)
    if oid is None:
        return jsonify({"error": "Invalid id"}), 400
    txns.delete_one({"_id": oid})
    return jsonify({"ok": True})

# -- Entries -----------------------------------------------
@app.route("/entries", methods=["GET"])
@require_auth
def get_entries():
    q = {}
    if request.args.get("platform"): q["platform"] = request.args["platform"]
    if request.args.get("stock"):    q["stock"]    = request.args["stock"]
    return jsonify([serialize(e) for e in entries.find(q).sort("date", DESCENDING)])

@app.route("/entries", methods=["POST"])
@require_auth
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
@require_auth
def delete_entry(eid):
    oid = safe_object_id(eid)
    if oid is None:
        return jsonify({"error": "Invalid id"}), 400
    entries.delete_one({"_id": oid})
    return jsonify({"ok": True})

# -- Summary -----------------------------------------------
@app.route("/summary", methods=["GET"])
@require_auth
def get_summary():
    latest = latest_entries_per_position()

    total_value    = sum(e["value"] for e in latest)
    total_invested = sum(e.get("invested", 0) for e in latest)

    # Daily gain: sum stored daily_gain from latest entries (vs prev close).
    # Denominator MUST exclude positions that lack a prev_close so we don't
    # divide by a partial total.
    contributing  = [e for e in latest if e.get("daily_gain") is not None]
    daily_gain    = round(sum(e["daily_gain"] for e in contributing), 2) if contributing else None
    prev_subtotal = sum(e["value"] - e["daily_gain"] for e in contributing)
    daily_pct     = (round(daily_gain / prev_subtotal * 100, 2)
                     if (daily_gain is not None and prev_subtotal > 0) else None)

    platforms, stocks = {}, {}
    for e in latest:
        platforms[e["platform"]] = round(platforms.get(e["platform"], 0) + e["value"], 2)
        stocks[e["stock"]]       = round(stocks.get(e["stock"], 0) + e["value"], 2)

    return jsonify({
        "total_value":      round(total_value, 2),
        "total_invested":   round(total_invested, 2),
        "total_gain":       round(total_value - total_invested, 2),
        "daily_gain":       daily_gain,
        "daily_gain_pct":   daily_pct,
        "daily_gain_basis": round(prev_subtotal, 2) if contributing else None,
        "platforms":        platforms,
        "stocks":           stocks,
        "entry_count":      entries.estimated_document_count(),
    })

@app.route("/platforms", methods=["GET"])
@require_auth
def get_platforms():
    return jsonify(sorted(entries.distinct("platform")))

@app.route("/stocks", methods=["GET"])
@require_auth
def get_stocks():
    return jsonify(sorted(entries.distinct("stock")))

# -- Refresh -----------------------------------------------
_refresh_lock = threading.Lock()  # in-process guard against double-clicks

def _run_refresh_async():
    """Wrap do_refresh in the in-process lock so one worker can't run it twice."""
    if not _refresh_lock.acquire(blocking=False):
        print("[Refresh] Already running, skipping")
        return
    try:
        do_refresh()
    except Exception as e:
        print(f"[Refresh] Failed: {e}")
    finally:
        _refresh_lock.release()

@app.route("/refresh-prices", methods=["POST"])
@require_auth
def refresh_prices():
    if _refresh_lock.locked():
        return jsonify({"started": False, "error": "Refresh already in progress"}), 409
    threading.Thread(target=_run_refresh_async, daemon=True).start()
    return jsonify({"started": True}), 202

@app.route("/refresh-status", methods=["GET"])
@require_auth
def refresh_status():
    m = meta.find_one({"key": "last_refresh"})
    in_progress = _refresh_lock.locked()
    if not m:
        return jsonify({"refreshed_today": False, "last_refresh": None, "in_progress": in_progress})
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return jsonify({
        "refreshed_today": m.get("date") == today,
        "last_refresh":    m.get("timestamp"),
        "date":            m.get("date"),
        "count":           m.get("count", 0),
        "in_progress":     in_progress,
    })

# -- Stock Analyzer ---------------------------------------
@app.route("/analyze/<symbol>", methods=["GET"])
@require_auth
def analyze(symbol):
    symbol = symbol.upper().strip()
    force = request.args.get("refresh", "false").lower() == "true"

    if not force:
        cached = analyses.find_one({"symbol": symbol}, sort=[("analyzed_at", DESCENDING)])
        if cached:
            age_h = (datetime.utcnow() - datetime.fromisoformat(cached["analyzed_at"])).total_seconds() / 3600
            if age_h < 24:
                cached["id"] = str(cached.pop("_id"))
                return jsonify({"cached": True, "age_hours": round(age_h, 1), "report": cached})

    if not FMP_KEY:
        return jsonify({"error": "FMP_API_KEY not configured"}), 503
    if not CLAUDE_KEY:
        return jsonify({"error": "ANTHROPIC_API_KEY not configured"}), 503

    try:
        from analyzer import analyze_stock
        from claude_synthesis import synthesize_full_report

        scores = analyze_stock(symbol)

        portfolio_context = None
        try:
            portfolio_context = [
                {"platform": e["platform"], "stock": e["stock"],
                 "value": e["value"], "shares": e.get("shares")}
                for e in latest_entries_per_position()
            ]
        except Exception as pe:
            print(f"[Analyzer] Could not load portfolio context: {pe}")

        narrative = synthesize_full_report(scores, portfolio_context)
        report = {
            "symbol": symbol,
            "analyzed_at": datetime.utcnow().isoformat(),
            "scores": scores,
            "narrative": narrative,
            "version": "1.0"
        }
        inserted = analyses.insert_one(dict(report))
        report["id"] = str(inserted.inserted_id)
        report.pop("_id", None)
        return jsonify({"cached": False, "report": report})

    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        print(f"[Analyzer] Failed for {symbol}: {e}")
        return jsonify({"error": f"Analysis failed: {str(e)}"}), 500


@app.route("/analyze/<symbol>/question", methods=["POST"])
@require_auth
def ask_question(symbol):
    symbol = symbol.upper().strip()
    d = request.get_json() or {}
    question = d.get("question", "").strip()
    if not question:
        return jsonify({"error": "No question provided"}), 400
    cached = analyses.find_one({"symbol": symbol}, sort=[("analyzed_at", DESCENDING)])
    if not cached:
        return jsonify({"error": "Run an analysis first"}), 404
    scores = cached.get("scores", {})
    profile = scores.get("profile", {})
    try:
        from claude_synthesis import claude_complete, SYSTEM_PROMPT
        prompt = f"""A beginner investor is reading an analysis of {profile.get('name', symbol)} and has a question.

Key facts (use only these):
- Overall score: {scores.get('overall_score','N/A')}/100
- Quality score: {scores.get('quality',{}).get('score','N/A')}/100
- Value trap risk: {scores.get('value_trap',{}).get('risk_level','N/A')}
- Sector: {profile.get('sector','N/A')}

Question: {question}

Answer in 2-4 sentences. Frame as educational context, not financial advice."""
        answer = claude_complete(prompt, SYSTEM_PROMPT, max_tokens=300)
        return jsonify({"answer": answer, "symbol": symbol})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/recent-analyses", methods=["GET"])
@require_auth
def recent_analyses():
    pipeline = [
        {"$sort": {"analyzed_at": -1}},
        {"$group": {"_id": "$symbol", "latest": {"$first": "$$ROOT"}}},
        {"$sort": {"latest.analyzed_at": -1}},
        {"$limit": 12}
    ]
    out = []
    for r in analyses.aggregate(pipeline):
        doc = r["latest"]
        out.append({
            "symbol": doc.get("symbol"),
            "company_name": doc.get("scores", {}).get("profile", {}).get("name"),
            "overall_score": doc.get("scores", {}).get("overall_score"),
            "verdict": doc.get("scores", {}).get("verdict"),
            "analyzed_at": doc.get("analyzed_at"),
        })
    return jsonify(out)


@app.route("/track-record", methods=["GET"])
@require_auth
def track_record():
    symbol = request.args.get("symbol", "").upper()
    query = {"symbol": symbol} if symbol else {}
    records = list(analyses.find(query, sort=[("analyzed_at", DESCENDING)]).limit(50))
    out = [{"id": str(r["_id"]), "symbol": r.get("symbol"), "analyzed_at": r.get("analyzed_at"),
            "overall_score": r.get("scores", {}).get("overall_score"),
            "verdict": r.get("scores", {}).get("verdict"),
            "company_name": r.get("scores", {}).get("profile", {}).get("name")} for r in records]
    return jsonify({"records": out, "total": len(out)})


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
