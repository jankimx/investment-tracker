from flask import Flask, request, jsonify, g, has_request_context
from flask_cors import CORS
from pymongo import MongoClient, DESCENDING, ASCENDING
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import wraps
import os, json, time, base64, secrets, urllib.request, urllib.parse, pytz, threading
from datetime import datetime, timedelta

load_dotenv()

app = Flask(__name__)
CORS(app, expose_headers=["Authorization"])

# -- Config ------------------------------------------------
MONGO_URI        = os.getenv("MONGO_URI", "mongodb://localhost:27017/")
DB_NAME          = os.getenv("DB_NAME", "investment_tracker")
APP_PASSWORD     = os.getenv("APP_PASSWORD")  # required, no default
RESEND_KEY       = os.getenv("RESEND_API_KEY", "")
NOTIFY_VERIZON   = os.getenv("NOTIFY_VERIZON", "")
NOTIFY_ATT       = os.getenv("NOTIFY_ATT", "")
APP_URL          = os.getenv("APP_URL", "https://investment-tracker-inky.vercel.app")
FMP_KEY          = os.getenv("FMP_API_KEY", "")
CLAUDE_KEY       = os.getenv("ANTHROPIC_API_KEY", "")

# Benchmark tickers the dashboard insights compare against. Pulled by the
# regular price refresh so they're cached in `prices` alongside holdings.
BENCHMARK_SYMBOLS = ["SPY", "QQQ", "VTI"]

if not APP_PASSWORD:
    raise RuntimeError("APP_PASSWORD env var must be set -- refusing to start with no password.")

SESSION_TTL_DAYS = 30

# -- Database ----------------------------------------------
client   = MongoClient(MONGO_URI)
db       = client[DB_NAME]
entries  = db["entries"]      # Legacy: per-day per-position snapshots (kept for backup; new code derives instead)
holdings = db["holdings"]     # Legacy: cached current positions (kept for backup)
txns     = db["transactions"] # Source of truth for buy/sell-tracked positions
prices   = db["prices"]       # NEW: global per-symbol per-day close prices
balances = db["balances"]     # NEW: source of truth for snapshot-tracked positions (401k etc.)
meta     = db["meta"]
analyses = db["analyses"]
sessions = db["sessions"]
news     = db["news"]        # per-symbol-per-day cache of stock_news headlines
# Per-card insights cache. Each collection holds {_id: "YYYY-MM-DD",
# version, generated_at, duration_ms, claude_calls, payload}. Keyed by
# card_id from insights.CARD_VERSIONS so adding a new card just adds a
# new collection key here. The legacy single `insights` collection is no
# longer read or written; old data in it can be dropped manually.
CARD_COLLECTIONS = {
    "concentration": db["insights_concentration"],
    "benchmark":     db["insights_benchmark"],
    "risk_news":     db["insights_risk_news"],
}

# Create indexes once at startup
try:
    entries.create_index([("date", DESCENDING)])
    entries.create_index([("platform", 1), ("stock", 1)])
    txns.create_index([("platform", 1), ("stock", 1), ("date", DESCENDING)])
    analyses.create_index([("symbol", 1), ("analyzed_at", DESCENDING)])
    sessions.create_index([("token", ASCENDING)], unique=True)
    # TTL index — Mongo deletes session docs once expires_at is in the past
    sessions.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    # TTL on meta.expires_at — used for insights generation locks (and any
    # future short-lived meta token). Docs without `expires_at` are unaffected.
    meta.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
except Exception as e:
    print(f"[Startup] Index warning: {e}")

# Unique constraints on logical keys. Wrapped separately so existing duplicates
# log a warning (and the app keeps booting) instead of crashing.
for spec, coll, name in [
    ([("platform", 1), ("stock", 1)], holdings, "holdings_unique"),
    ([("date", 1), ("platform", 1), ("stock", 1)], entries, "entries_unique"),
    ([("symbol", 1), ("date", 1)], prices,   "prices_unique"),
    ([("platform", 1), ("stock", 1), ("date", 1)], balances, "balances_unique"),
    ([("symbol", 1), ("date", 1)], news,     "news_unique"),
]:
    try:
        coll.create_index(spec, unique=True, name=name)
    except Exception as e:
        print(f"[Startup] Could not create unique index {name} (likely existing duplicate data): {e}")

# Non-unique indexes for common query paths
try:
    prices.create_index([("symbol", 1), ("date", DESCENDING)])
    balances.create_index([("platform", 1), ("stock", 1), ("date", DESCENDING)])
except Exception as e:
    print(f"[Startup] secondary index warning: {e}")

# -- One-time migration ------------------------------------
def run_migration():
    """Idempotent migration from the legacy entries/holdings model into the
    new prices + transactions + balances model. Safe to call on every startup;
    re-runs are cheap and no-op once everything is in place.

    Steps:
      1. Backfill `prices` from any auto-refreshed entries that have a price.
         Keyed by (symbol, date) -- duplicates skip.
      2. For every existing holding that has no matching transaction history,
         synthesize an "initial position" buy at the recorded cost basis.
      3. Mark migration progress in `meta` so we don't re-scan every boot.
    """
    state = meta.find_one({"key": "schema_migration_v2"}) or {}
    if state.get("done"):
        return

    print("[Migration] Starting v2 schema migration")

    # 1) Backfill prices from entries that have a numeric price field.
    price_count = 0
    for e in entries.find(
        {"price": {"$exists": True, "$ne": None}, "stock": {"$exists": True}, "date": {"$exists": True}},
        {"_id": 0, "stock": 1, "date": 1, "price": 1, "updated_at": 1},
    ):
        symbol = (e["stock"] or "").upper()
        if not symbol:
            continue
        try:
            prices.update_one(
                {"symbol": symbol, "date": e["date"]},
                {"$setOnInsert": {
                    "symbol":     symbol,
                    "date":       e["date"],
                    "close":      float(e["price"]),
                    "source":     "migrated_from_entries",
                    "fetched_at": e.get("updated_at") or datetime.utcnow().isoformat(),
                }},
                upsert=True,
            )
            price_count += 1
        except DuplicateKeyError:
            pass
        except Exception as ex:
            print(f"[Migration] price upsert failed for {symbol} {e.get('date')}: {ex}")
    print(f"[Migration] Backfilled {price_count} price rows")

    # 2) Synthesize seed transactions for orphan holdings.
    #    NEW model: shares is signed (positive = buy, negative = sell).
    #    No `action` field on transactions.
    seed_count = 0
    for h in holdings.find():
        platform = h.get("platform")
        stock    = (h.get("stock") or "").upper()
        shares   = float(h.get("shares", 0))
        cb       = float(h.get("cost_basis", 0))
        if not platform or not stock or shares <= 0:
            continue
        if txns.count_documents({"platform": platform, "stock": stock}, limit=1) > 0:
            continue  # has real transaction history; leave alone
        txns.insert_one({
            "platform": platform, "stock": stock,
            "shares": shares,  # positive = buy
            "price_per_share": cb,
            "date": (h.get("updated_at") or datetime.utcnow().isoformat())[:10],
            "created_at": datetime.utcnow().isoformat(),
            "synthetic": True,
            "note": "Seeded by v2 migration from legacy holdings row",
        })
        seed_count += 1
    print(f"[Migration] Seeded {seed_count} synthetic transactions for orphan holdings")

    # 3) Convert legacy action=buy/sell transactions to signed shares.
    flipped = 0
    for t in txns.find({"action": {"$exists": True}}):
        update = {"$unset": {"action": ""}}
        cur_shares = float(t.get("shares", 0))
        if t.get("action") == "sell" and cur_shares > 0:
            update["$set"] = {"shares": -cur_shares}
            flipped += 1
        txns.update_one({"_id": t["_id"]}, update)
    if flipped:
        print(f"[Migration] Converted {flipped} 'sell' transactions to negative shares")
    print("[Migration] Removed `action` field from legacy transactions")

    meta.update_one(
        {"key": "schema_migration_v2"},
        {"$set": {"key": "schema_migration_v2", "done": True,
                  "completed_at": datetime.utcnow().isoformat(),
                  "prices_seeded": price_count, "txns_seeded": seed_count,
                  "sells_flipped": flipped}},
        upsert=True,
    )
    print("[Migration] v2 migration complete")

try:
    run_migration()
except Exception as e:
    print(f"[Migration] Failed (will retry next boot): {e}")


def cleanup_superseded_balances():
    """Delete balance rows whose (platform, stock) is now covered by a
    transaction-tracked position. Cheap and idempotent -- safe to call on
    every startup and after every new transaction."""
    txn_keys = []
    for r in txns.aggregate([
        {"$group": {"_id": {"platform": "$platform", "stock": "$stock"}}}
    ]):
        txn_keys.append((r["_id"]["platform"], r["_id"]["stock"]))
    if not txn_keys:
        return 0
    deleted = 0
    for (platform, stock) in txn_keys:
        result = balances.delete_many({"platform": platform, "stock": stock})
        deleted += result.deleted_count
    if deleted:
        print(f"[Cleanup] Deleted {deleted} balance rows superseded by transactions")
    return deleted


def migrate_share_balances_to_transactions():
    """Convert legacy share-tracked balance rows into synthetic 'buy'
    transactions and remove the balance row. Idempotent: a balance row that
    has shares > 0 AND a real ticker (stock != 'TOTAL') is migrated; one
    with shares=null/0 or stock='TOTAL' is left alone (those are value-only
    snapshots and belong in `balances`)."""
    converted = 0
    for b in list(balances.find({"shares": {"$gt": 0}})):
        platform = b.get("platform")
        stock    = (b.get("stock") or "").upper()
        if not platform or not stock or stock == "TOTAL":
            continue
        # If a transaction already exists for this position, the balance row
        # is just dead duplicate -- drop it.
        if txns.count_documents({"platform": platform, "stock": stock}, limit=1) > 0:
            balances.delete_one({"_id": b["_id"]})
            continue
        shares     = float(b.get("shares", 0))
        cost_basis = float(b.get("cost_basis") or 0)
        txns.insert_one({
            "platform": platform, "stock": stock,
            "shares": shares,
            "price_per_share": cost_basis,
            "date": b.get("date") or datetime.utcnow().strftime("%Y-%m-%d"),
            "created_at": datetime.utcnow().isoformat(),
            "synthetic": True,
            "note": "Migrated from legacy share-tracked balance row",
        })
        balances.delete_one({"_id": b["_id"]})
        converted += 1
    if converted:
        print(f"[Migration] Converted {converted} share-tracked balances to transactions")
    return converted

def drop_deprecated_insights_collection():
    """One-time cleanup of the legacy single-doc `insights` collection. The
    feature now uses one collection per card type (insights_concentration
    etc.); old data is dead weight. Idempotent — no-op once the collection
    is gone."""
    try:
        if "insights" in db.list_collection_names():
            db.drop_collection("insights")
            print("[Cleanup] Dropped deprecated `insights` collection")
    except Exception as e:
        print(f"[Cleanup] Could not drop deprecated `insights` collection: {e}")


try:
    migrate_share_balances_to_transactions()
    cleanup_superseded_balances()
    drop_deprecated_insights_collection()
except Exception as e:
    print(f"[Cleanup] Failed (non-fatal): {e}")

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

def position_id(platform, stock):
    """URL-safe stable id for a (platform, stock) pair. Used so the existing
    DELETE /holdings/<id> route can address a derived position even though
    there's no longer a backing `holdings` document."""
    raw = f"{platform}||{stock.upper()}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")

def decode_position_id(pid):
    """Inverse of position_id. Returns (platform, stock) or (None, None)."""
    if not pid:
        return None, None
    pad = "=" * (-len(pid) % 4)
    try:
        raw = base64.urlsafe_b64decode(pid + pad).decode()
        platform, sep, stock = raw.partition("||")
        if platform and sep and stock:
            return platform, stock
    except Exception:
        pass
    return None, None

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

def _fetch_one_price(symbol):
    """Hit FMP's stable /quote endpoint for a single symbol.
    Returns (symbol, price_or_None, error_or_None). Matches analyzer.py's
    proven-working endpoint shape (same /stable base, same query-param style)."""
    try:
        url = "https://financialmodelingprep.com/stable/quote?" + urllib.parse.urlencode(
            {"symbol": symbol, "apikey": FMP_KEY}
        )
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        if isinstance(data, dict) and ("Error Message" in data or "error" in data):
            return symbol, None, str(data.get("Error Message") or data.get("error"))
        # /stable/quote returns a list with one entry per symbol.
        if isinstance(data, list) and data:
            price = data[0].get("price")
            if price is not None:
                return symbol, float(price), None
            return symbol, None, "No 'price' field in FMP response"
        return symbol, None, f"Unexpected FMP response: {str(data)[:160]}"
    except Exception as e:
        return symbol, None, str(e)

def fetch_prices_batch(symbols):
    """Fetch current prices for many symbols in parallel.
    Returns (prices_by_symbol, errors_by_symbol, top_level_error).
    - prices_by_symbol: SYM -> float price (only successful fetches)
    - errors_by_symbol: SYM -> error string (only failed fetches)
    - top_level_error: set only on config failures (e.g. no FMP_KEY)."""
    if not FMP_KEY:
        return {}, {}, "FMP_API_KEY not configured"
    if not symbols:
        return {}, {}, None
    out_prices = {}
    out_errors = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(_fetch_one_price, s) for s in symbols]
        for fut in as_completed(futures):
            sym, price, err = fut.result()
            if price is not None:
                out_prices[sym] = price
            else:
                out_errors[sym] = err or "Unknown error"
                print(f"[Refresh] {sym} fetch error: {err}")
    return out_prices, out_errors, None

def do_refresh(notify=False):
    """Fetch latest price for every distinct symbol we track and upsert
    into `prices`. Source of "what to fetch" is transactions + balances
    (i.e. anything we currently hold). One batched FMP call for all tickers.
    SMS is sent only if `notify=True` (reserved for the 16:00 ET close tick)."""
    today   = datetime.utcnow().strftime("%Y-%m-%d")
    results = []
    errors  = []

    positions = derive_all_positions()
    # Skip value-only balance entries (e.g. 401k aggregate rows where stock=="TOTAL"
    # or any balance with no shares). They have no real ticker to fetch.
    held_symbols = {
        p["stock"].upper() for p in positions
        if p.get("shares") is not None and p["stock"].upper() != "TOTAL"
    }
    # Always include benchmark tickers so the insights card can compare even
    # if the user holds none of them directly.
    symbols = sorted(held_symbols | set(BENCHMARK_SYMBOLS))
    if not symbols:
        meta.update_one(
            {"key": "last_refresh"},
            {"$set": {
                "key": "last_refresh", "timestamp": datetime.utcnow().isoformat(),
                "date": today, "count": 0,
                "errors": [{"stock": "*", "error": "No tracked symbols (transactions/balances empty)"}],
            }},
            upsert=True,
        )
        return {"refreshed": 0, "errors": [], "date": today}

    prices_by_sym, errors_by_sym, batch_err = fetch_prices_batch(symbols)
    if batch_err:
        print(f"[Refresh] Batch fetch failed: {batch_err}")
        errors.append({"stock": "*", "error": batch_err})
    else:
        fetched_at = datetime.utcnow().isoformat()
        for symbol in symbols:
            price = prices_by_sym.get(symbol)
            if price is None:
                errors.append({"stock": symbol, "error": errors_by_sym.get(symbol, "No price returned by FMP")})
                continue
            prices.update_one(
                {"symbol": symbol, "date": today},
                {"$set": {
                    "symbol": symbol, "date": today,
                    "close": float(price),
                    "source": "fmp",
                    "fetched_at": fetched_at,
                }},
                upsert=True,
            )
            results.append({"stock": symbol, "close": price})

    now_utc = datetime.utcnow()
    # Always record the attempt so /refresh-status can surface failures.
    meta.update_one(
        {"key": "last_refresh"},
        {"$set": {
            "key": "last_refresh",
            "timestamp": now_utc.isoformat(),
            "date": today,
            "count": len(results),
            "errors": errors[:10],  # cap to avoid unbounded growth
        }},
        upsert=True
    )

    if notify and results:
        et_tz   = pytz.timezone("America/New_York")
        et_time = pytz.utc.localize(now_utc).astimezone(et_tz)
        send_sms(et_time.strftime("%I:%M %p").lstrip("0"))

    print(f"[Refresh] Done: {len(results)} refreshed, {len(errors)} errors, notify={notify}")
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
        return
    # SMS only on the 16:00 ET close tick. Intraday minute ticks are silent.
    is_close = (et_now.hour == 16 and et_now.minute == 0)
    print(f"[Scheduler] Worker pid={os.getpid()} won tick {tick_id}, running refresh (notify={is_close})")
    _run_refresh_async(notify=is_close)

scheduler = BackgroundScheduler()
# Per-minute refresh during US equities regular session: Mon-Fri 9:30am-4:00pm ET.
# Cron 'minute=*' runs every minute of the included hours; we restrict hour 9 to
# minutes 30-59 and hour 16 to minute 0 (the close itself) so we stay inside the
# 9:30-16:00 window.
ET_TZ = pytz.timezone("America/New_York")
scheduler.add_job(
    _scheduled_refresh,
    CronTrigger(day_of_week="mon-fri", hour="10-15", minute="*", timezone=ET_TZ),
    coalesce=True, max_instances=1, id="refresh_intraday",
)
scheduler.add_job(
    _scheduled_refresh,
    CronTrigger(day_of_week="mon-fri", hour=9, minute="30-59", timezone=ET_TZ),
    coalesce=True, max_instances=1, id="refresh_open",
)
scheduler.add_job(
    _scheduled_refresh,
    CronTrigger(day_of_week="mon-fri", hour=16, minute=0, timezone=ET_TZ),
    coalesce=True, max_instances=1, id="refresh_close",
)
# Daily insights generation — 16:30 ET, after the close-tick refresh has
# populated today's prices. Decoupled from refresh so a failure in one
# doesn't block the other. See INSIGHTS_DESIGN.md §5.
scheduler.add_job(
    lambda: _scheduled_insights(),
    CronTrigger(day_of_week="mon-fri", hour=16, minute=30, timezone=ET_TZ),
    coalesce=True, max_instances=1, id="insights_daily",
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


# -- Derivation helpers (new schema: prices + transactions + balances) --
#
# These functions compute the views the UI needs from the source-of-truth
# tables, replacing the old cached `holdings` and `entries` collections.

def get_latest_price(symbol):
    """Latest close for a symbol, or None if no prices recorded."""
    p = prices.find_one({"symbol": symbol.upper()}, sort=[("date", DESCENDING)])
    return float(p["close"]) if p and p.get("close") is not None else None


def get_price_on(symbol, date):
    """Close price on the given YYYY-MM-DD, or the most recent prior date if
    that exact day wasn't fetched. Used to value historical positions for the
    chart -- handles weekends, holidays, and rate-limit gaps naturally."""
    p = prices.find_one(
        {"symbol": symbol.upper(), "date": {"$lte": date}},
        sort=[("date", DESCENDING)],
    )
    return float(p["close"]) if p and p.get("close") is not None else None


def get_prev_price(symbol, ref_date=None):
    """Close from the trading day BEFORE ref_date (default: today). Used for
    daily-gain math -- pairs with get_latest_price()."""
    if ref_date is None:
        ref_date = datetime.utcnow().strftime("%Y-%m-%d")
    p = prices.find_one(
        {"symbol": symbol.upper(), "date": {"$lt": ref_date}},
        sort=[("date", DESCENDING)],
    )
    return (float(p["close"]) if p and p.get("close") is not None else None,
            p["date"] if p else None)


def compute_position_from_transactions(platform, stock):
    """Walk the transaction history for (platform, stock) and return
    (shares, cost_basis). `shares` on each transaction is signed: positive =
    buy, negative = sell. Buys roll into a weighted-average cost basis; sells
    subtract shares only (avg-cost convention)."""
    history = list(txns.find(
        {"platform": platform, "stock": stock.upper()}
    ).sort("date", ASCENDING))
    shares = 0.0
    cost_basis = 0.0
    for t in history:
        s = float(t.get("shares", 0))
        p = float(t.get("price_per_share", 0))
        if s > 0:  # buy
            new_shares = shares + s
            cost_basis = ((shares * cost_basis) + (s * p)) / new_shares if new_shares > 0 else p
            shares = new_shares
        elif s < 0:  # sell
            shares = max(0.0, shares + s)  # s is negative, so this subtracts
            # cost basis unchanged on sells (avg-cost convention)
    return shares, cost_basis


def shares_as_of(platform, stock, date):
    """Net shares held in (platform, stock) at end-of-day on `date`. For chart
    series reconstruction. Uses signed shares -- just sum, clamp at 0."""
    history = list(txns.find(
        {"platform": platform, "stock": stock.upper(), "date": {"$lte": date}}
    ).sort("date", ASCENDING))
    shares = 0.0
    for t in history:
        shares += float(t.get("shares", 0))
    return max(0.0, shares)


def derive_all_positions():
    """Return a list of dicts describing every currently-held position.
    Optimized for one page-load: 3 bulk Mongo queries total (all transactions,
    all balances, latest 2 prices per symbol) instead of N-per-position
    round-trips. Memoized for the duration of a single Flask request -- but
    safe to call from background threads (the cache is skipped if there is
    no active request context)."""
    in_request = has_request_context()
    if in_request:
        cached = getattr(g, "_positions_cache", None)
        if cached is not None:
            return cached

    # 1) Load every transaction in one shot, group in Python.
    txn_by_key = {}  # (platform, stock) -> list of txn dicts (date-ordered)
    for t in txns.find(
        {},
        {"_id": 0, "platform": 1, "stock": 1, "shares": 1,
         "price_per_share": 1, "date": 1, "action": 1},
    ).sort("date", ASCENDING):
        key = (t.get("platform"), (t.get("stock") or "").upper())
        if not key[0] or not key[1]:
            continue
        txn_by_key.setdefault(key, []).append(t)

    # 2) Load latest balance per (platform, stock) in one aggregation.
    latest_balance = {}
    for r in balances.aggregate([
        {"$sort":  {"date": DESCENDING}},
        {"$group": {"_id": {"platform": "$platform", "stock": "$stock"},
                    "latest": {"$first": "$$ROOT"}}},
    ]):
        b = r["latest"]
        key = (b.get("platform"), (b.get("stock") or "").upper())
        if key[0] and key[1]:
            latest_balance[key] = b

    # 3) For each distinct symbol, load the two most recent prices (today's
    #    close + prev close) in one bulk aggregation.
    all_symbols = {key[1] for key in txn_by_key.keys()} | {key[1] for key in latest_balance.keys()}
    price_pairs = {}  # symbol -> (latest_close, prev_close)
    if all_symbols:
        for r in prices.aggregate([
            {"$match": {"symbol": {"$in": list(all_symbols)}}},
            {"$sort":  {"symbol": 1, "date": DESCENDING}},
            {"$group": {"_id": "$symbol",
                        "closes": {"$push": "$close"}}},
        ]):
            closes = r.get("closes") or []
            price_pairs[r["_id"]] = (
                float(closes[0]) if len(closes) >= 1 and closes[0] is not None else None,
                float(closes[1]) if len(closes) >= 2 and closes[1] is not None else None,
            )

    # Walk transactions in-memory to compute (shares, cost_basis) per position
    def walk(history):
        shares = 0.0
        cb     = 0.0
        for t in history:
            s = float(t.get("shares", 0))
            p = float(t.get("price_per_share", 0))
            # Legacy compat: action=sell + positive shares -> negative shares.
            if t.get("action") == "sell" and s > 0:
                s = -s
            if s > 0:
                ns = shares + s
                cb = ((shares * cb) + (s * p)) / ns if ns > 0 else p
                shares = ns
            elif s < 0:
                shares = max(0.0, shares + s)
        return shares, cb

    positions = []
    txn_keys  = set()
    for key, history in txn_by_key.items():
        platform, stock = key
        shares, cb = walk(history)
        if shares <= 0:
            continue
        txn_keys.add(key)
        positions.append({
            "platform": platform, "stock": stock,
            "shares": round(shares, 6), "cost_basis": round(cb, 4),
            "source": "transactions",
        })

    for key, b in latest_balance.items():
        if key in txn_keys:
            continue
        platform, stock = key
        shares_raw = b.get("shares")
        is_value_only = shares_raw is None or stock == "TOTAL"
        shares = float(shares_raw) if shares_raw is not None else 0.0
        # Skip positions where the user zeroed out share-tracked balances;
        # but for value-only entries we only care about value > 0.
        if not is_value_only and shares <= 0:
            continue
        if is_value_only and not (b.get("value") or 0):
            continue
        positions.append({
            "platform": platform, "stock": stock,
            "shares": round(shares, 6) if not is_value_only else None,
            "cost_basis": round(float(b.get("cost_basis") or 0), 4) if not is_value_only else None,
            "source": "balance",
            "value_only": is_value_only,
            "user_reported_value": b.get("value"),
            "user_reported_invested": b.get("invested"),
        })

    # Enrich with price + value + daily gain (no Mongo calls -- in-memory lookup)
    for pos in positions:
        sym    = pos["stock"].upper()
        shares = pos.get("shares") or 0.0
        cb     = pos.get("cost_basis") or 0.0
        is_value_only = pos.pop("value_only", False)

        if is_value_only:
            # Value-only balance: trust the user-reported total directly,
            # don't try price math.
            pos["price"]      = None
            pos["value"]      = round(float(pos.get("user_reported_value") or 0), 2)
            pos["invested"]   = round(float(pos.get("user_reported_invested") or 0), 2)
            pos["shares"]     = None
            pos["cost_basis"] = None
            pos["prev_close"]     = None
            pos["daily_gain"]     = None
            pos["daily_gain_pct"] = None
        else:
            price, prev = price_pairs.get(sym, (None, None))
            if price is not None:
                pos["price"]    = round(price, 4)
                pos["value"]    = round(price * shares, 2)
            else:
                pos["price"] = None
                pos["value"] = (round(float(pos.get("user_reported_value") or 0), 2)
                                if pos.get("source") == "balance" else None)
            pos["invested"] = round(cb * shares, 2)

            if price is not None and prev is not None and prev > 0:
                pos["prev_close"]     = round(prev, 4)
                pos["daily_gain"]     = round((price - prev) * shares, 2)
                pos["daily_gain_pct"] = round((price - prev) / prev * 100, 2)
            else:
                pos["prev_close"]     = None
                pos["daily_gain"]     = None
                pos["daily_gain_pct"] = None
        pos.pop("user_reported_value", None)
        pos.pop("user_reported_invested", None)

    if in_request:
        g._positions_cache = positions
    return positions


def backfill_prices_from_fmp(symbol, since_date):
    """Pull daily close bars for `symbol` from FMP starting at `since_date`
    and upsert into prices. Idempotent -- existing (symbol, date) rows are
    not overwritten. Returns the number of new rows inserted, or 0 if no key
    is configured."""
    if not FMP_KEY:
        return 0
    symbol = symbol.upper()
    try:
        url = (f"https://financialmodelingprep.com/stable/historical-price-eod/light"
               f"?symbol={symbol}&from={since_date}&apikey={FMP_KEY}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
    except Exception as ex:
        print(f"[FMP backfill] {symbol} since={since_date}: {ex}")
        return 0

    # FMP returns either a list of {date, price/close, volume} or {historical: [...]}
    rows = data if isinstance(data, list) else (data.get("historical") or [])
    inserted = 0
    for row in rows:
        date  = row.get("date")
        close = row.get("price") or row.get("close") or row.get("adjClose")
        if not date or close is None:
            continue
        try:
            prices.update_one(
                {"symbol": symbol, "date": date},
                {"$setOnInsert": {
                    "symbol":     symbol,
                    "date":       date,
                    "close":      float(close),
                    "source":     "fmp_historical",
                    "fetched_at": datetime.utcnow().isoformat(),
                }},
                upsert=True,
            )
            inserted += 1
        except DuplicateKeyError:
            pass
        except Exception as ex:
            print(f"[FMP backfill] upsert failed for {symbol} {date}: {ex}")
    print(f"[FMP backfill] {symbol}: fetched {inserted} rows since {since_date}")
    return inserted


def ensure_price_history_covers(symbol, since_date):
    """If our earliest prices row for `symbol` is later than `since_date`,
    fetch FMP historical bars back to that date. No-op if coverage already
    extends to (or before) since_date."""
    earliest = prices.find_one({"symbol": symbol.upper()}, sort=[("date", ASCENDING)])
    if earliest and earliest.get("date", "9999-99-99") <= since_date:
        return
    backfill_prices_from_fmp(symbol, since_date)


def derive_chart_series(since_date, platform_filter=None):
    """Return slim {date, platform, stock, value} rows over a date window.

    Optimized for one page-load: 3 bulk Mongo queries instead of
    O(positions × dates) round-trips. Strategy:
      1. Load every transaction (any date) and every balance into memory.
      2. Load every price >= since_date into a {symbol: [(date, close), ...]} map.
      3. Walk each (platform, stock) position once: maintain a running share
         count, step through trading dates, multiply by that day's close.
    """
    # 1) Load all transactions in one query, group by (platform, stock).
    txn_by_key = {}
    for t in txns.find(
        {},
        {"_id": 0, "platform": 1, "stock": 1, "shares": 1, "action": 1, "date": 1},
    ).sort("date", ASCENDING):
        key = (t.get("platform"), (t.get("stock") or "").upper())
        if not key[0] or not key[1]:
            continue
        if platform_filter and key[0] != platform_filter:
            continue
        txn_by_key.setdefault(key, []).append(t)

    # 2) Load all balances in one query, sorted ascending. Keep both shares
    #    (for share-tracked snapshots) and value (for value-only snapshots).
    bal_by_key = {}
    for b in balances.find(
        {},
        {"_id": 0, "platform": 1, "stock": 1, "shares": 1, "value": 1, "date": 1},
    ).sort("date", ASCENDING):
        key = (b.get("platform"), (b.get("stock") or "").upper())
        if not key[0] or not key[1]:
            continue
        if platform_filter and key[0] != platform_filter:
            continue
        bal_by_key.setdefault(key, []).append(b)

    keys = set(txn_by_key.keys()) | set(bal_by_key.keys())
    if not keys:
        return []

    symbols = {k[1] for k in keys}

    # 3) Load every price in the window, plus the most recent price BEFORE the
    #    window per symbol (so day 1 of the chart can be valued even if its
    #    bar landed before since_date).
    prices_by_sym = {s: [] for s in symbols}
    for p in prices.find(
        {"symbol": {"$in": list(symbols)}, "date": {"$gte": since_date}},
        {"_id": 0, "symbol": 1, "date": 1, "close": 1},
    ).sort("date", ASCENDING):
        prices_by_sym.setdefault(p["symbol"], []).append((p["date"], float(p["close"])))

    # Pre-window seed for forward-fill: most recent close strictly before
    # since_date, per symbol.
    seed_close = {}
    for r in prices.aggregate([
        {"$match": {"symbol": {"$in": list(symbols)}, "date": {"$lt": since_date}}},
        {"$sort":  {"date": DESCENDING}},
        {"$group": {"_id": "$symbol", "close": {"$first": "$close"},
                    "date": {"$first": "$date"}}},
    ]):
        seed_close[r["_id"]] = float(r["close"])

    # Set of dates we'll plot: every distinct date in prices within the window,
    # plus every balance snapshot date within the window (so value-only
    # positions can render even with no price data).
    date_set = {d for series in prices_by_sym.values() for (d, _) in series}
    for bal_hist in bal_by_key.values():
        for b in bal_hist:
            d = b.get("date")
            if d and d >= since_date:
                date_set.add(d)
    distinct_dates = sorted(date_set)
    if not distinct_dates:
        return []

    out = []
    for (platform, stock) in sorted(keys):
        sym = stock
        history = txn_by_key.get((platform, stock), [])
        bal_hist = bal_by_key.get((platform, stock), [])
        is_balance_only = not history and bool(bal_hist)

        # Value-only balances are flagged by stock == "TOTAL" OR any balance
        # entry that has value but no shares. Use the dollar value directly,
        # skip all price math.
        is_value_only = is_balance_only and any(
            (b.get("shares") in (None, 0)) and (b.get("value") is not None)
            for b in bal_hist
        )

        if is_value_only:
            # Step through balance snapshots in chronological order; forward-
            # fill the latest reported value across distinct_dates.
            current_value = None
            bal_idx = 0
            # Seed from any snapshot before since_date
            while bal_idx < len(bal_hist) and bal_hist[bal_idx].get("date", "") < since_date:
                if bal_hist[bal_idx].get("value") is not None:
                    current_value = float(bal_hist[bal_idx]["value"])
                bal_idx += 1
            for d in distinct_dates:
                while bal_idx < len(bal_hist) and bal_hist[bal_idx].get("date", "") <= d:
                    if bal_hist[bal_idx].get("value") is not None:
                        current_value = float(bal_hist[bal_idx]["value"])
                    bal_idx += 1
                if current_value is None or current_value <= 0:
                    continue
                out.append({
                    "date":     d,
                    "platform": platform,
                    "stock":    stock,
                    "value":    round(current_value, 2),
                })
            continue

        # Share-tracked path (transactions OR share-based balances)
        running_shares = 0.0
        txn_idx = 0
        bal_idx = 0
        sym_prices = prices_by_sym.get(sym, [])
        price_idx  = 0
        current_price = seed_close.get(sym)

        if not is_balance_only:
            for t in history:
                if t.get("date", "") >= since_date:
                    break
                s = float(t.get("shares", 0))
                if t.get("action") == "sell" and s > 0:
                    s = -s
                running_shares += s
                txn_idx += 1
            running_shares = max(0.0, running_shares)

        if is_balance_only:
            for b in bal_hist:
                if b.get("date", "") < since_date:
                    running_shares = float(b.get("shares") or 0)
                    bal_idx += 1
                else:
                    break

        for d in distinct_dates:
            if not is_balance_only:
                while txn_idx < len(history) and history[txn_idx].get("date", "") <= d:
                    s = float(history[txn_idx].get("shares", 0))
                    if history[txn_idx].get("action") == "sell" and s > 0:
                        s = -s
                    running_shares += s
                    txn_idx += 1
                running_shares = max(0.0, running_shares)

            if is_balance_only:
                while bal_idx < len(bal_hist) and bal_hist[bal_idx].get("date", "") <= d:
                    running_shares = float(bal_hist[bal_idx].get("shares") or 0)
                    bal_idx += 1

            while price_idx < len(sym_prices) and sym_prices[price_idx][0] <= d:
                current_price = sym_prices[price_idx][1]
                price_idx += 1

            if running_shares <= 0 or current_price is None:
                continue
            out.append({
                "date":     d,
                "platform": platform,
                "stock":    stock,
                "value":    round(current_price * running_shares, 2),
            })

    return out


# -- Holdings (derived) ------------------------------------
# `holdings` is no longer a stored collection -- it's a view over transactions
# and balances. GET returns derived positions; POST writes a transaction (or
# balance); DELETE wipes all records for a (platform, stock) pair.

@app.route("/holdings", methods=["GET"])
@require_auth
def get_holdings():
    rows = []
    for p in derive_all_positions():
        rows.append({
            "id":         position_id(p["platform"], p["stock"]),
            "platform":   p["platform"],
            "stock":      p["stock"],
            "shares":     p["shares"],
            "cost_basis": p["cost_basis"],
            "source":     p["source"],
        })
    return jsonify(rows)

@app.route("/holdings", methods=["POST"])
@require_auth
def upsert_holding():
    """Convenience endpoint: 'I have X shares of Y at cost basis Z'.
    Writes a synthetic 'buy' transaction (or a balance if explicitly asked).
    """
    d = request.get_json() or {}
    for f in ["platform", "stock", "shares"]:
        if f not in d:
            return jsonify({"error": f"Missing: {f}"}), 400
    platform   = d["platform"]
    stock      = d["stock"].upper()
    shares     = float(d["shares"])
    cost_basis = float(d.get("cost_basis", 0))
    if shares <= 0:
        return jsonify({"error": "shares must be positive"}), 400

    # If there's no existing transaction history, seed it with a synthetic
    # 'buy'. Otherwise the user is asking us to overwrite an existing
    # position, which we no longer support directly -- they should use
    # /transactions or /balances explicitly.
    has_history  = txns.count_documents({"platform": platform, "stock": stock}, limit=1) > 0
    has_balance  = balances.count_documents({"platform": platform, "stock": stock}, limit=1) > 0
    if has_history or has_balance:
        return jsonify({
            "error": "Position already exists; use /transactions or /balances to update it"
        }), 409

    txns.insert_one({
        "platform": platform, "stock": stock, "action": "buy",
        "shares": shares, "price_per_share": cost_basis,
        "date": datetime.utcnow().strftime("%Y-%m-%d"),
        "created_at": datetime.utcnow().isoformat(),
        "synthetic": True,
        "note": "Initial position seeded from /holdings upsert",
    })
    return jsonify({"ok": True}), 201

@app.route("/holdings/<hid>", methods=["DELETE"])
@require_auth
def delete_holding(hid):
    """Remove a position entirely: wipe its transaction log AND any balance
    snapshots. Permanent -- the Frontend's confirm dialog should make this
    clear."""
    platform, stock = decode_position_id(hid)
    if not platform:
        return jsonify({"error": "Invalid id"}), 400
    t_del = txns.delete_many({"platform": platform, "stock": stock}).deleted_count
    b_del = balances.delete_many({"platform": platform, "stock": stock}).deleted_count
    return jsonify({"ok": True, "transactions_deleted": t_del, "balances_deleted": b_del})

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
    """Accepts either:
      { platform, stock, shares (signed), price_per_share, date? }    -- new
      { platform, stock, action: "buy"|"sell", shares (positive), ... } -- legacy
    Stores only signed shares; no `action` field."""
    d = request.get_json() or {}
    for f in ["platform", "stock", "shares", "price_per_share"]:
        if f not in d:
            return jsonify({"error": f"Missing: {f}"}), 400

    platform = d["platform"]
    stock    = d["stock"].upper()
    shares   = float(d["shares"])
    pps      = float(d["price_per_share"])
    date     = d.get("date", datetime.utcnow().strftime("%Y-%m-%d"))

    # Legacy compat: if an action field was sent alongside positive shares,
    # apply the sign.
    action = d.get("action")
    if action == "sell" and shares > 0:
        shares = -shares
    elif action == "buy" and shares < 0:
        shares = abs(shares)

    if shares == 0:
        return jsonify({"error": "shares must be non-zero"}), 400

    txns.insert_one({
        "platform": platform, "stock": stock,
        "shares": shares,
        "price_per_share": pps,
        "date": date,
        "created_at": datetime.utcnow().isoformat(),
    })

    # Any pre-existing balance row for this same position is now superseded.
    balances.delete_many({"platform": platform, "stock": stock})

    # If this transaction is dated before our earliest price for the symbol,
    # backfill historical bars from FMP so the chart can render from that date.
    try:
        ensure_price_history_covers(stock, date)
    except Exception as ex:
        print(f"[Transactions] backfill failed for {stock} since {date}: {ex}")

    return jsonify({"ok": True}), 201

@app.route("/transactions/<tid>", methods=["DELETE"])
@require_auth
def delete_transaction(tid):
    oid = safe_object_id(tid)
    if oid is None:
        return jsonify({"error": "Invalid id"}), 400
    txns.delete_one({"_id": oid})
    return jsonify({"ok": True})

# -- Balances ----------------------------------------------
# For snapshot-tracked positions (401k, HSA, target-date funds...) where you
# only have periodic statements rather than per-trade data.
@app.route("/balances", methods=["GET"])
@require_auth
def get_balances():
    q = {}
    if request.args.get("platform"): q["platform"] = request.args["platform"]
    if request.args.get("stock"):    q["stock"]    = request.args["stock"].upper()
    rows = [serialize(b) for b in balances.find(q).sort("date", DESCENDING)]
    # Hide balance rows that conflict with a transaction-tracked position for
    # the same (platform, stock). The dashboard already ignores them; surface
    # the same filtering here so the Balances tab only shows active records.
    txn_keys = set()
    for r in txns.aggregate([
        {"$group": {"_id": {"platform": "$platform", "stock": "$stock"}}}
    ]):
        txn_keys.add((r["_id"]["platform"], r["_id"]["stock"]))
    rows = [b for b in rows if (b.get("platform"), b.get("stock")) not in txn_keys]
    return jsonify(rows)

@app.route("/balances", methods=["POST"])
@require_auth
def add_balance():
    """Two supported shapes:

    Value-only (the common 401k case -- you just have a dollar amount):
        { platform: "<name>", date, value, invested? }
        Internally stored with stock="TOTAL", shares=null.

    Share-tracked (you know the exact ticker + share count):
        { platform, stock, date, shares, cost_basis?, value? }
    """
    d = request.get_json() or {}
    for f in ["platform", "date"]:
        if f not in d:
            return jsonify({"error": f"Missing: {f}"}), 400
    if "value" not in d and "shares" not in d:
        return jsonify({"error": "Provide at least one of: value, shares"}), 400

    platform = d["platform"]
    date     = d["date"]
    stock    = (d.get("stock") or "TOTAL").upper()
    shares   = float(d["shares"]) if d.get("shares") not in (None, "") else None
    cb       = float(d.get("cost_basis")) if d.get("cost_basis") not in (None, "") else None
    value    = float(d["value"]) if d.get("value") not in (None, "") else None
    invested = float(d.get("invested")) if d.get("invested") not in (None, "") else None
    note     = d.get("note", "")

    # Block conflict with transaction-tracked positions on the SAME key.
    if stock != "TOTAL" and txns.count_documents(
            {"platform": platform, "stock": stock}, limit=1) > 0:
        return jsonify({"error": "This position is transaction-tracked; use /transactions instead"}), 409

    balances.update_one(
        {"platform": platform, "stock": stock, "date": date},
        {"$set": {
            "platform": platform, "stock": stock, "date": date,
            "shares": shares, "cost_basis": cb,
            "value": value, "invested": invested,
            "note": note,
            "created_at": datetime.utcnow().isoformat(),
        }},
        upsert=True,
    )

    # Only meaningful to backfill prices for real tickers, not the "TOTAL" sentinel
    if stock and stock != "TOTAL":
        try:
            ensure_price_history_covers(stock, date)
        except Exception as ex:
            print(f"[Balances] backfill failed for {stock} since {date}: {ex}")

    return jsonify({"ok": True}), 201

@app.route("/balances/<bid>", methods=["DELETE"])
@require_auth
def delete_balance(bid):
    oid = safe_object_id(bid)
    if oid is None:
        return jsonify({"error": "Invalid id"}), 400
    balances.delete_one({"_id": oid})
    return jsonify({"ok": True})

# -- Entries -----------------------------------------------
ENTRIES_DEFAULT_LIMIT = 200
ENTRIES_MAX_LIMIT     = 2000

@app.route("/entries", methods=["GET"])
@require_auth
def get_entries():
    """Paginated list of entries, newest first.

    Query params:
        platform, stock  -- optional filters
        limit            -- max rows to return (default 200, hard cap 2000)
        before           -- only entries with date strictly less than this
                            YYYY-MM-DD (used to page backwards in time)
    """
    q = {}
    if request.args.get("platform"): q["platform"] = request.args["platform"]
    if request.args.get("stock"):    q["stock"]    = request.args["stock"]
    if request.args.get("before"):   q["date"]     = {"$lt": request.args["before"]}

    try:
        limit = int(request.args.get("limit", ENTRIES_DEFAULT_LIMIT))
    except ValueError:
        limit = ENTRIES_DEFAULT_LIMIT
    limit = max(1, min(limit, ENTRIES_MAX_LIMIT))

    cursor = entries.find(q).sort("date", DESCENDING).limit(limit)
    return jsonify([serialize(e) for e in cursor])

@app.route("/positions", methods=["GET"])
@require_auth
def get_positions():
    """All currently-held positions, fully enriched with current price,
    value, daily gain. Source-of-truth is transactions + balances + prices."""
    out = []
    for p in derive_all_positions():
        out.append({
            "id":             position_id(p["platform"], p["stock"]),
            "platform":       p["platform"],
            "stock":          p["stock"],
            "shares":         p["shares"],
            "cost_basis":     p["cost_basis"],
            "invested":       p.get("invested"),
            "price":          p.get("price"),
            "prev_close":     p.get("prev_close"),
            "value":          p.get("value"),
            "daily_gain":     p.get("daily_gain"),
            "daily_gain_pct": p.get("daily_gain_pct"),
            "source":         p.get("source"),
        })
    return jsonify(out)

CHART_DEFAULT_DAYS = 90
CHART_MAX_DAYS     = 730

@app.route("/chart-data", methods=["GET"])
@require_auth
def get_chart_data():
    """Slim time series for the dashboard chart, derived from
    transactions + balances × prices. Returns one row per
    (date, platform, stock) over the requested window."""
    try:
        days = int(request.args.get("days", CHART_DEFAULT_DAYS))
    except ValueError:
        days = CHART_DEFAULT_DAYS
    days = max(1, min(days, CHART_MAX_DAYS))

    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows  = derive_chart_series(since)
    return jsonify({"since": since, "days": days, "rows": rows})

@app.route("/entries", methods=["POST"])
@require_auth
def add_entry():
    d = request.get_json() or {}
    for f in ["date", "platform", "stock", "value"]:
        if f not in d:
            return jsonify({"error": f"Missing: {f}"}), 400
    d["stock"]      = d["stock"].upper()
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
    positions = derive_all_positions()

    total_value    = sum((p.get("value") or 0) for p in positions)
    total_invested = sum((p.get("invested") or 0) for p in positions)

    contributing  = [p for p in positions if p.get("daily_gain") is not None]
    daily_gain    = round(sum(p["daily_gain"] for p in contributing), 2) if contributing else None
    prev_subtotal = sum((p.get("value") or 0) - (p.get("daily_gain") or 0) for p in contributing)
    daily_pct     = (round(daily_gain / prev_subtotal * 100, 2)
                     if (daily_gain is not None and prev_subtotal > 0) else None)

    platforms, stocks = {}, {}
    for p in positions:
        v = p.get("value") or 0
        platforms[p["platform"]] = round(platforms.get(p["platform"], 0) + v, 2)
        stocks[p["stock"]]       = round(stocks.get(p["stock"], 0) + v, 2)

    return jsonify({
        "total_value":      round(total_value, 2),
        "total_invested":   round(total_invested, 2),
        "total_gain":       round(total_value - total_invested, 2),
        "daily_gain":       daily_gain,
        "daily_gain_pct":   daily_pct,
        "daily_gain_basis": round(prev_subtotal, 2) if contributing else None,
        "platforms":        platforms,
        "stocks":           stocks,
        "position_count":   len(positions),
    })

@app.route("/platforms", methods=["GET"])
@require_auth
def get_platforms():
    s = set(txns.distinct("platform"))
    s.update(balances.distinct("platform"))
    return jsonify(sorted(p for p in s if p))

@app.route("/stocks", methods=["GET"])
@require_auth
def get_stocks():
    s = set(t.upper() for t in txns.distinct("stock") if t)
    s.update(b.upper() for b in balances.distinct("stock") if b)
    return jsonify(sorted(s))

# -- Refresh -----------------------------------------------
_refresh_lock = threading.Lock()  # in-process guard against double-clicks

def _run_refresh_async(notify=False):
    """Wrap do_refresh in the in-process lock so one worker can't run it twice.
    On exception, persist the failure to meta.last_refresh so /refresh-status
    can surface it — otherwise the error is only visible in server logs."""
    if not _refresh_lock.acquire(blocking=False):
        print("[Refresh] Already running, skipping")
        return
    try:
        do_refresh(notify=notify)
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[Refresh] Failed: {e}\n{tb}")
        try:
            meta.update_one(
                {"key": "last_refresh"},
                {"$set": {
                    "key": "last_refresh",
                    "timestamp": datetime.utcnow().isoformat(),
                    "date": datetime.utcnow().strftime("%Y-%m-%d"),
                    "count": 0,
                    "errors": [{"stock": "*", "error": f"do_refresh exception: {type(e).__name__}: {e}"}],
                }},
                upsert=True,
            )
        except Exception as inner:
            print(f"[Refresh] Could not record failure to meta: {inner}")
    finally:
        _refresh_lock.release()

@app.route("/refresh-prices", methods=["POST"])
@require_auth
def refresh_prices():
    if _refresh_lock.locked():
        return jsonify({"started": False, "error": "Refresh already in progress"}), 409
    # Manual refresh is silent — user is already in the app.
    threading.Thread(target=_run_refresh_async, kwargs={"notify": False}, daemon=True).start()
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
        "refreshed_today": m.get("date") == today and m.get("count", 0) > 0,
        "last_refresh":    m.get("timestamp"),
        "date":            m.get("date"),
        "count":           m.get("count", 0),
        "errors":          m.get("errors", []),
        "in_progress":     in_progress,
    })

# -- Insights ----------------------------------------------
# Dashboard CTA card row. See INSIGHTS_DESIGN.md.
#
# Cron generates the daily doc at 16:30 ET. Reads lazily regenerate if the
# doc is missing (weekends, outages, fresh deploy). Concurrent generation is
# de-duped by a TTL-protected lock document in `meta`.

# Per-card in-process locks so a single worker can't accidentally spawn two
# threads for the same (date, card). Lazy-init keyed by card_id.
_card_local_locks = {}


def _local_card_lock(card_id):
    lock = _card_local_locks.get(card_id)
    if lock is None:
        lock = threading.Lock()
        _card_local_locks[card_id] = lock
    return lock


def _today_str():
    return datetime.utcnow().strftime("%Y-%m-%d")


def _claim_card_lock(today, card_id):
    """Atomically claim the cross-worker lock for one card's generation on
    `today`. Returns (claimed, started_at). Other workers see DuplicateKeyError
    and return claimed=False; their started_at is the existing lock's timestamp."""
    now = datetime.utcnow()
    lock_id = f"insights_lock_{card_id}_{today}"
    try:
        meta.insert_one({
            "_id":        lock_id,
            "started_at": now,
            "expires_at": now + timedelta(seconds=180),  # generous; risk card can take a while
            "pid":        os.getpid(),
            "card_id":    card_id,
        })
        return True, now
    except DuplicateKeyError:
        existing = meta.find_one({"_id": lock_id})
        return False, (existing.get("started_at") if existing else now)


def _release_card_lock(today, card_id):
    meta.delete_one({"_id": f"insights_lock_{card_id}_{today}"})


NEWS_WINDOW_DAYS = 7
NEWS_LIMIT_PER_SYMBOL = 10


def fetch_news(symbol, since_date=None, limit=NEWS_LIMIT_PER_SYMBOL):
    """Return today's cached news items for `symbol`. On cache miss, hits FMP
    /stock_news, normalizes the response, and persists to the news collection
    for the rest of the day. Returns a list of items (possibly empty)."""
    symbol = symbol.upper().strip()
    today  = datetime.utcnow().strftime("%Y-%m-%d")
    cached = news.find_one({"symbol": symbol, "date": today})
    if cached:
        return cached.get("items", [])

    if not FMP_KEY:
        return []

    since = since_date or (datetime.utcnow() - timedelta(days=NEWS_WINDOW_DAYS)).strftime("%Y-%m-%d")
    try:
        url = ("https://financialmodelingprep.com/api/v3/stock_news"
               f"?tickers={symbol}&from={since}&limit={limit}&apikey={FMP_KEY}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
    except Exception as ex:
        print(f"[News] fetch failed for {symbol}: {ex}")
        return []

    if not isinstance(data, list):
        return []

    items = []
    for n in data[:limit]:
        title = (n.get("title") or "").strip()
        link  = (n.get("url") or "").strip()
        if not title or not link:
            continue
        # Stable ID lets the Claude verification pass cite a specific source.
        nid = "news_" + base64.urlsafe_b64encode(link.encode()).decode().rstrip("=")[:20]
        items.append({
            "id":        nid,
            "title":     title,
            "url":       link,
            "published": n.get("publishedDate"),
            "site":      n.get("site"),
            "summary":   (n.get("text") or "")[:500],
        })

    try:
        news.replace_one(
            {"symbol": symbol, "date": today},
            {
                "_id":        f"{symbol}_{today}",
                "symbol":     symbol,
                "date":       today,
                "fetched_at": datetime.utcnow().isoformat(),
                "items":      items,
            },
            upsert=True,
        )
    except Exception as ex:
        print(f"[News] cache upsert failed for {symbol}: {ex}")

    return items


def _ytd_start_date():
    """First calendar day of the current year as YYYY-MM-DD (UTC)."""
    return datetime.utcnow().strftime("%Y") + "-01-01"


def _load_benchmark_data_for_insights():
    """Build the inputs that insights.compute_benchmark_comparison() expects.

    Returns (portfolio_totals_by_date, benchmark_closes_by_symbol). May raise
    on price-fetch failures -- caller (insights.generate_benchmark_card)
    catches and skips the benchmark card."""
    ytd_start = _ytd_start_date()

    # Make sure each benchmark has historical bars back to YTD. ensure_price_
    # history_covers is idempotent and a no-op if we already have coverage.
    for sym in BENCHMARK_SYMBOLS:
        try:
            ensure_price_history_covers(sym, ytd_start)
        except Exception as ex:
            print(f"[Insights/benchmark] backfill failed for {sym}: {ex}")

    # Portfolio total per date over the YTD window.
    chart_rows = derive_chart_series(ytd_start)
    portfolio_totals = {}
    for r in chart_rows:
        d = r["date"]
        portfolio_totals[d] = portfolio_totals.get(d, 0.0) + float(r.get("value") or 0)

    # Load benchmark closes (date -> close) for each available benchmark.
    benchmark_closes = {}
    for sym in BENCHMARK_SYMBOLS:
        closes = {}
        for p in prices.find(
            {"symbol": sym, "date": {"$gte": ytd_start}},
            {"_id": 0, "date": 1, "close": 1},
        ):
            if p.get("close") is not None:
                closes[p["date"]] = float(p["close"])
        if closes:
            benchmark_closes[sym] = closes

    return portfolio_totals, benchmark_closes


def _build_card_closures(positions):
    """Wire up the Claude + DB callables that the per-card generators need.
    Constructed once per worker call so we don't re-import on every card."""
    from insights import collect_risk_inputs, synthesize_risk_card_verified
    from claude_synthesis import (
        summarize_concentration,
        summarize_benchmark,
        extract_risk_items,
        verify_risk_items,
        synthesize_risk_prose,
    )

    def prose_fn(card_id, stats):
        if card_id == "concentration":
            return summarize_concentration(stats)
        if card_id == "benchmark":
            return summarize_benchmark(stats)
        return None

    def get_latest_analysis(sym):
        return analyses.find_one({"symbol": sym}, sort=[("analyzed_at", DESCENDING)])

    def risk_pipeline():
        inputs = collect_risk_inputs(positions, fetch_news, get_latest_analysis)
        if not inputs.get("by_symbol"):
            return None
        return synthesize_risk_card_verified(
            inputs,
            extract_fn=extract_risk_items,
            verify_fn=verify_risk_items,
            synthesize_fn=synthesize_risk_prose,
        )

    return {
        "prose_fn":     prose_fn if CLAUDE_KEY else None,
        "benchmark_fn": _load_benchmark_data_for_insights,
        "risk_fn":      risk_pipeline if CLAUDE_KEY else None,
    }


def _generate_one_card_and_persist(today, card_id, trigger):
    """Generate one card type and upsert into its collection. Idempotent: if
    another thread/worker is generating this card, we exit. Always releases
    both the in-process and Mongo lock on exit."""
    local_lock = _local_card_lock(card_id)
    if not local_lock.acquire(blocking=False):
        print(f"[Insights/{card_id}] in-process generation already running, skipping")
        return
    try:
        from insights import (
            CARD_VERSIONS,
            generate_concentration_card,
            generate_benchmark_card,
            generate_risk_news_card,
        )

        started   = datetime.utcnow()
        positions = derive_all_positions()
        closures  = _build_card_closures(positions)

        if card_id == "concentration":
            payload, calls = generate_concentration_card(positions, closures["prose_fn"])
        elif card_id == "benchmark":
            payload, calls = generate_benchmark_card(
                prose_fn=closures["prose_fn"],
                benchmark_fn=closures["benchmark_fn"],
            )
        elif card_id == "risk_news":
            payload, calls = generate_risk_news_card(closures["risk_fn"])
        else:
            print(f"[Insights] Unknown card_id '{card_id}' — skipping")
            return

        finished    = datetime.utcnow()
        duration_ms = int((finished - started).total_seconds() * 1000)

        CARD_COLLECTIONS[card_id].replace_one(
            {"_id": today},
            {
                "_id":          today,
                "version":      CARD_VERSIONS[card_id],
                "generated_at": started.isoformat(),
                "duration_ms":  duration_ms,
                "claude_calls": calls,
                "trigger":      trigger,
                "payload":      payload,  # may be None (computed but no card to render)
            },
            upsert=True,
        )
        shown = bool(payload and payload.get("show"))
        print(
            f"[Insights/{card_id}] {trigger} done in {duration_ms}ms "
            f"(claude_calls={calls}, show={shown})"
        )
    except Exception as e:
        import traceback
        print(f"[Insights/{card_id}] generation failed: {type(e).__name__}: {e}\n{traceback.format_exc()}")
    finally:
        _release_card_lock(today, card_id)
        local_lock.release()


def _spawn_card_generation(today, card_id, trigger):
    """Claim the lock for (today, card_id) and spawn a worker if we got it.
    Returns the started_at timestamp of whichever lock is currently held."""
    claimed, started_at = _claim_card_lock(today, card_id)
    if claimed:
        threading.Thread(
            target=_generate_one_card_and_persist,
            args=(today, card_id, trigger),
            daemon=True,
        ).start()
    return started_at


def _scheduled_insights():
    """Daily cron — 16:30 ET Mon-Fri. Forces regeneration of every card so
    each gets a fresh take on today's news/prices/positions. Per-card locks
    let the three cards generate concurrently."""
    from insights import CARD_IDS
    today = _today_str()
    print(f"[Insights] pid={os.getpid()} cron firing for {today}")
    for card_id in CARD_IDS:
        # Force regen even if today's doc already matches version — cron is
        # the daily "freshen everything" pass.
        _release_card_lock(today, card_id)
        _spawn_card_generation(today, card_id, trigger="cron")


@app.route("/insights/dashboard", methods=["GET"])
@require_auth
def insights_dashboard():
    """Per-card response: each entry has its own status (ready or generating).
    Cards that have been generated but didn't trigger (payload=None) are
    omitted entirely. Frontend continues polling while any entry is generating.

    Backwards-compat: if a card collection has no doc for today (or its
    cached version is stale relative to CARD_VERSIONS) we kick off a
    background regeneration just for that card."""
    from insights import CARD_VERSIONS, CARD_IDS, CARD_DISPLAY_NAMES, CARD_LOADING_MESSAGES
    today = _today_str()
    entries     = []
    generating  = []
    newest_ts   = None

    for card_id in CARD_IDS:
        coll = CARD_COLLECTIONS[card_id]
        doc  = coll.find_one({"_id": today})
        if doc and doc.get("version") == CARD_VERSIONS[card_id]:
            ts = doc.get("generated_at")
            if ts and (newest_ts is None or ts > newest_ts):
                newest_ts = ts
            payload = doc.get("payload")
            if payload and payload.get("show"):
                entry = dict(payload)
                entry["id"]      = card_id
                entry["status"]  = "ready"
                entries.append(entry)
            # payload missing or show=False -> omit from response, frontend
            # learns it doesn't render
        else:
            # Stale or missing -- spawn background generation and surface a
            # placeholder so the frontend can render a labeled loading slot.
            _spawn_card_generation(today, card_id, trigger="lazy")
            entries.append({
                "id":              card_id,
                "status":          "generating",
                "display_name":    CARD_DISPLAY_NAMES.get(card_id, card_id),
                "loading_message": CARD_LOADING_MESSAGES.get(card_id, "Generating…"),
            })
            generating.append(card_id)

    return jsonify({
        "as_of":      newest_ts,
        "cards":      entries,
        "generating": generating,
    })


@app.route("/insights/risk-news/<symbol>", methods=["GET"])
@require_auth
def insights_risk_news(symbol):
    """Drill-down for a single holding's news + analyzer signals. Used by the
    risk card's drawer so the user can read the underlying sources directly."""
    symbol = symbol.upper().strip()
    items = fetch_news(symbol)
    analysis = analyses.find_one({"symbol": symbol}, sort=[("analyzed_at", DESCENDING)])

    signals = []
    if analysis:
        from insights import _extract_analyzer_signals  # lazy import to avoid cycle
        signals = _extract_analyzer_signals(symbol, analysis)

    return jsonify({
        "symbol":  symbol,
        "news":    items,
        "signals": signals,
    })


@app.route("/benchmark/<symbol>", methods=["GET"])
@require_auth
def benchmark_series(symbol):
    """Historical close prices for a benchmark ticker, used by the dashboard
    to overlay on the growth chart. Default window matches /chart-data
    (90 days back from today)."""
    symbol = symbol.upper().strip()
    if symbol not in BENCHMARK_SYMBOLS:
        return jsonify({
            "error": f"Unsupported benchmark; choose one of {BENCHMARK_SYMBOLS}",
        }), 400

    from_date = request.args.get("from") or (
        datetime.utcnow() - timedelta(days=CHART_DEFAULT_DAYS)
    ).strftime("%Y-%m-%d")
    to_date = request.args.get("to") or datetime.utcnow().strftime("%Y-%m-%d")

    try:
        ensure_price_history_covers(symbol, from_date)
    except Exception as ex:
        print(f"[Benchmark] backfill failed for {symbol}: {ex}")

    rows = []
    for p in prices.find(
        {"symbol": symbol, "date": {"$gte": from_date, "$lte": to_date}},
        {"_id": 0, "date": 1, "close": 1},
    ).sort("date", ASCENDING):
        if p.get("close") is not None:
            rows.append({"date": p["date"], "close": float(p["close"])})

    return jsonify({
        "symbol": symbol,
        "from":   from_date,
        "to":     to_date,
        "series": rows,
    })


@app.route("/insights/refresh", methods=["POST"])
@require_auth
def insights_refresh():
    """Force regenerate today's insights for all cards. Returns 202 immediately
    with the list of cards whose generation was spawned."""
    from insights import CARD_IDS
    today = _today_str()
    spawned = []
    for card_id in CARD_IDS:
        # Drop the cached doc and any stale lock so we can re-claim cleanly.
        CARD_COLLECTIONS[card_id].delete_one({"_id": today})
        _release_card_lock(today, card_id)
        _spawn_card_generation(today, card_id, trigger="manual")
        spawned.append(card_id)
    return jsonify({"status": "generating", "generating": spawned}), 202


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
        "fmp":           "configured" if FMP_KEY else "missing -- add FMP_API_KEY",
        "claude":        "configured" if CLAUDE_KEY else "missing -- add ANTHROPIC_API_KEY",
        "scheduler":     "running"
    })

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
