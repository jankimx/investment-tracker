# Insights Dashboard — Design Doc

*Drafted 2026-05-17. Status: spec, not yet implemented.*

This doc captures the design for the AI insights feature on the dashboard. Three actionable cards above the hero metrics, generated daily by cron, with a verification pass for the news-driven card.

---

## 1. Goals

Surface three call-to-action cards at the top of the dashboard that help drive decisions, not just describe state. Each card hides itself when there's nothing to act on.

| # | Card | Trigger condition | Action |
|---|---|---|---|
| 1 | **Performance vs benchmark** | `abs(portfolio_pct − benchmark_pct) ≥ 2pp` (YTD) | Overlay benchmark series on the growth chart + show delta |
| 2 | **Allocation / concentration** | `top_1_pct > 25%` OR `top_3_pct > 50%` OR any sector `> 60%` | Open drawer with breakdown by stock, sector, platform |
| 3 | **Risky stocks (news + analyzer signals)** | ≥1 item classified `critical`, or ≥3 items total | Open drawer with per-symbol upside/downside summary |

---

## 2. UX

```
┌────────────────────────────────────────────────────────────────────┐
│ [⚠ Concentration]  [📉 Trailing SPY]  [🚩 3 holdings flagged]     │  ← CTA row (new)
└────────────────────────────────────────────────────────────────────┘
[Total value] [Invested] [Total gain] [Today]                          ← hero metrics (unchanged)
[chart controls / growth chart / positions table ...]                  ← unchanged
```

**Rules:**
- Row only renders cards where `show: true`. If all three are false, the row collapses entirely.
- Cards are sorted critical → warn → info regardless of array order.
- Card 1's CTA is in-place (chart overlay). Cards 2 and 3 open a **right-side drawer**.
- Drawer footer always includes provenance: data sources used, generation time, and deep-links to relevant tabs.
- Dismissibility / snooze is **out of scope for v1** — revisit if cards become noisy.

---

## 3. Data model

### 3.1 New collection: `insights`

One document per day. Read on every dashboard load.

```jsonc
{
  "_id": "2026-05-17",                          // date string, primary key
  "generated_at": "2026-05-17T20:32:11Z",       // ISO 8601 UTC
  "generation": {
    "trigger": "cron",                          // "cron" | "lazy"
    "duration_ms": 12500,
    "claude_calls": 5                           // 1 benchmark + 1 concentration + 3 risk/news
  },

  "inputs": {
    "holdings_count": 12,
    "prices_as_of":   "2026-05-17",
    "analyses_used":  ["AAPL", "NVDA", "TSLA"],
    "news_window_days": 7,
    "risk_news_extract": { /* intermediate JSON from extract pass — see §6 */ }
  },

  "cards": [
    // ─── CARD 1: BENCHMARK ───
    {
      "id": "benchmark",
      "show": true,
      "severity": "info",                       // info | warn | critical
      "headline": "Trailing SPY by 2.3% YTD",
      "cta_label": "Compare on chart",
      "prose": "Your portfolio is up 8.1% YTD vs SPY's 10.4% — tech allocation doing the heavy lifting but cash drag pulling the total down.",
      "detail": {
        "default_benchmark": "SPY",
        "available_benchmarks": ["SPY", "QQQ", "VTI"],
        "period": { "from": "2026-01-01", "to": "2026-05-17", "label": "YTD" },
        "comparisons": [
          { "benchmark": "SPY", "portfolio_pct": 8.1, "benchmark_pct": 10.4, "delta_pp": -2.3 },
          { "benchmark": "QQQ", "portfolio_pct": 8.1, "benchmark_pct": 13.7, "delta_pp": -5.6 },
          { "benchmark": "VTI", "portfolio_pct": 8.1, "benchmark_pct":  9.8, "delta_pp": -1.7 }
        ],
        "trigger": { "rule": "abs_delta_pp_gt", "threshold": 2.0, "actual": 2.3 }
      }
    },

    // ─── CARD 2: CONCENTRATION ───
    {
      "id": "concentration",
      "show": true,
      "severity": "warn",
      "headline": "AAPL is 32% of your portfolio",
      "cta_label": "View allocation",
      "prose": "AAPL alone drives almost a third of your returns; top 3 names are 61% of total value. Single-name risk dominates diversification benefit.",
      "detail": {
        "top_holdings": [
          { "symbol": "AAPL", "value": 48210.50, "weight_pct": 32.1, "sector": "Technology" }
          // ...all holdings, sorted by weight desc
        ],
        "concentration": {
          "top_1_pct": 32.1,
          "top_3_pct": 61.1,
          "top_5_pct": 78.4,
          "hhi": 0.21                           // Herfindahl index, 0=diverse, 1=single name
        },
        "sectors":   [ { "sector": "Technology", "weight_pct": 71.3 } /* ... */ ],
        "platforms": [ { "platform": "Jon - Fidelity", "weight_pct": 58.0 } /* ... */ ],
        "trigger": { "rule": "top_1_pct_gt", "threshold": 25.0, "actual": 32.1 }
      }
    },

    // ─── CARD 3: RISK / NEWS ───
    {
      "id": "risk_news",
      "show": true,
      "severity": "critical",
      "headline": "3 holdings have notable activity",
      "cta_label": "View summary",
      "prose": "TSLA facing fresh regulatory scrutiny while AAPL got a major upgrade. NVDA continues to show insider selling — re-check theses on all three.",
      "verification": {
        "passes": 2,                            // 2 if first verify passed; 3 if a retry was needed
        "claims_total": 5,
        "claims_grounded": 5,
        "warnings": []                          // non-empty → drawer shows "⚠ auto-verified" badge
      },
      "detail": {
        "items": [
          {
            "symbol": "TSLA",
            "direction": "downside",            // upside | downside
            "severity": "critical",
            "summary": "DOJ probe into autonomy claims widened on May 15.",
            "sources": [
              { "type": "news",     "title": "DOJ expands Tesla autonomy probe", "url": "https://...", "published": "2026-05-15" },
              { "type": "analyzer", "signal": "red_flag", "detail": "Cash flow declining 3 quarters" }
            ]
          }
          // ...more items
        ],
        "trigger": { "rule": "critical_items_gte", "threshold": 1, "actual": 1 }
      }
    }
  ]
}
```

### 3.2 New collection: `news`

Mirrors the `prices` pattern. Per-symbol, per-date document so multiple consumers (the daily insights generator, the drawer drill-down) reuse the same fetch.

```jsonc
{
  "_id": "AAPL_2026-05-17",
  "symbol": "AAPL",
  "date":   "2026-05-17",
  "fetched_at": "2026-05-17T20:30:02Z",
  "items": [
    {
      "id": "fmp_4821",                        // stable ID for source-citation
      "title": "MS upgrades Apple to Overweight",
      "url": "https://...",
      "published": "2026-05-16T13:45:00Z",
      "site": "Bloomberg",
      "summary": "..."
    }
    // ...
  ]
}
```

### 3.3 Reuses

- `prices` — gains rows for SPY / QQQ / VTI so the same `fetch_prices_batch` pulls them alongside holdings.
- `analyses` — card #3 pulls cached `red_flag`, `value_trap`, and `insider_activity` signals from the analyzer's existing cache.

### 3.4 Indexes

```js
db.insights.createIndex({ _id: 1 })             // primary, implicit
db.insights.createIndex({ generated_at: -1 })   // debug "show me last N days"
db.news.createIndex({ symbol: 1, date: -1 })    // drill-down lookups
```

---

## 4. Read path

`GET /insights/dashboard` is the only call the frontend makes for the CTA row. It returns one of two states:

```jsonc
// Ready — the common path
{ "status": "ready",      "as_of": "2026-05-17T20:32:11Z", "cards": [...] }

// Generating — lazy fallback was triggered, work is in progress
{ "status": "generating", "started_at": "2026-05-17T22:15:01Z", "cards": [] }
```

```
GET /insights/dashboard
  ├─ insights[today] exists?
  │     ├─ yes → return { status: "ready", cards: [...] }
  │     └─ no  → claim lock + spawn background generation, return { status: "generating" }
  │              (if another request already holds the lock, just return "generating")
```

Filtered server-side to only include cards with `show: true`. Frontend has no business logic — it renders what it gets.

### 4.1 Frontend polling

When the first response is `status: "generating"`, the frontend renders a placeholder strip and polls the same endpoint with **exponential backoff**:

```
attempt:  1    2    3    4    5    6    7+
wait:     1s   2s   4s   8s   16s  32s  60s (capped)
```

Polling continues while the tab is open. No hard timeout — backoff caps at 60s so it doesn't hammer. On `status: "ready"`, the strip is replaced with the actual cards.

```
┌────────────────────────────────────────────────────────────────────┐
│ 🔄 Generating today's insights — usually ready in 10-15 seconds.   │
└────────────────────────────────────────────────────────────────────┘
```

A single full-width strip (not three skeleton cards) is used because card count isn't known yet — three skeletons collapsing to one would feel broken.

### 4.2 Concurrent-generation protection

The lazy-generation lock lives in the existing `meta` collection. Each lock document carries an explicit `expires_at` ~120s in the future, and a TTL index on that field auto-deletes the doc once it's in the past — so a crashed worker doesn't leave a stuck lock:

```jsonc
{
  "_id":        "insights_lock_2026-05-17",
  "started_at": ISODate("2026-05-17T22:15:01Z"),
  "expires_at": ISODate("2026-05-17T22:17:01Z"),
  "pid":        12345
}
```

```js
db.meta.createIndex({ expires_at: 1 }, { expireAfterSeconds: 0 })
```

Why `expireAfterSeconds: 0` on `expires_at` instead of a per-collection TTL on `started_at`? MongoDB's TTL index applies to every doc in the collection that has the indexed field as a date — but existing meta docs (`refresh_tick_*`, `schema_migration_v2`, etc.) don't have `expires_at`, so the TTL ignores them. Cleaner than filtering by an `_id` regex (which `partialFilterExpression` doesn't support anyway).

First request that misses inserts the lock and spawns the worker thread; subsequent requests see the lock (`DuplicateKeyError`) and return `generating` without spawning a second worker. When the worker finishes it deletes the lock. If the process crashes mid-generation, Mongo cleans the lock up automatically once `expires_at` passes.

---

## 5. Write path

### 5.1 Cron-driven (the common case)

Daily APScheduler job runs at **16:30 ET Mon–Fri**. Decoupled from the price-refresh cron so failures in one don't block the other.

```python
scheduler.add_job(_scheduled_insights, "cron",
                  day_of_week="mon-fri",
                  hour=16, minute=30,
                  timezone="America/New_York")

def _scheduled_insights():
    today = date.today().isoformat()
    try:
        meta.insert_one({"_id": f"insights_tick_{today}"})  # multi-worker dedup
    except DuplicateKeyError:
        return
    generate_insights(today)
```

### 5.2 Lazy fallback

If the cron didn't run (weekend, holiday, outage, fresh deploy), the first dashboard load of the day generates inline. Same `generate_insights()` function, same per-process lock that the existing `do_refresh()` uses to prevent concurrent doubles.

**Trade-off the user accepted:** weekends and holidays trigger a ~10s first-load on Saturday/Sunday because we chose freshness over caching Friday's doc. Acceptable for a personal app.

### 5.3 The generator

```python
def generate_insights(today):
    holdings   = load_holdings_with_prices()
    benchmarks = fetch_or_cache_prices(["SPY", "QQQ", "VTI"])
    news       = fetch_or_cache_news([h.symbol for h in holdings])
    analyses   = load_cached_analyses([h.symbol for h in holdings])

    # Pure-math (cheap, deterministic):
    concentration_stats = compute_concentration(holdings)
    benchmark_stats     = compute_relative_return(holdings, benchmarks)
    risk_items_raw      = collate_signals(news, analyses)

    # AI synthesis:
    prose_benchmark     = claude_summarize_benchmark(benchmark_stats)
    prose_concentration = claude_summarize_concentration(concentration_stats)
    risk_card           = generate_risk_news_verified(news, analyses, risk_items_raw)  # see §6

    doc = build_cards(concentration_stats, benchmark_stats, risk_card,
                      prose_benchmark, prose_concentration)
    insights.replace_one({"_id": today}, {"_id": today, **doc}, upsert=True)
    return doc
```

---

## 6. Verification pattern (risk/news card)

News is the highest-hallucination-risk input. Hard invariant: **the system never ships unverified output.** Either every claim is grounded in a source, or we ship a reduced/conservative result, or we ship nothing.

```
┌─────────────────────────────────────────────────────────┐
│  loop up to N=5 attempts:                               │
│     ┌─────────┐   ┌─────────┐                           │
│     │  Write  │ → │  Verify │                           │
│     └─────────┘   └─────────┘                           │
│          ↑              │                               │
│          │  pass? ──────┤                               │
│          │              │                               │
│          │  no: feed critique back into next Write      │
│          │  yes: break, go synthesize                   │
│                                                         │
│  if still not verified after N:                         │
│     ┌──────────────────┐   ┌────────┐                   │
│     │ CONSERVATIVE     │ → │ Verify │  (must pass)      │
│     │ Write (strict    │   └────────┘                   │
│     │ exact-citation   │                                │
│     │ items only)      │                                │
│     └──────────────────┘                                │
│                                                         │
│  ┌────────────┐                                         │
│  │ Synthesize │  (from validated items only)            │
│  └────────────┘                                         │
└─────────────────────────────────────────────────────────┘
```

**Write** (`temperature ~0.3`): Given news + analyzer signals, output structured JSON: per-symbol direction, severity, summary, and `source_id`s used. Prompt explicitly: *"Use only the provided inputs. Do not infer facts not present in the sources."*

**Verify** (`temperature 0`): Given the original inputs and the written JSON, confirm every claim cites a real source. Returns `{ grounded: bool, unsupported_claims: [...] }`. If grounded, exit loop.

**Conservative fallback**: If after `N=5` write/verify cycles the output still isn't grounded, switch to a stricter prompt that only allows items where the source citation is trivially exact (direct title match in news, exact analyzer signal name). This output must verify cleanly — if it produces zero items, the card just doesn't render.

**Synthesize** (`temperature ~0.5`): Given the *validated* JSON only (never the raw news), write the user-facing prose. Cannot introduce new facts.

### Invariant: shipped output is always verified

Three possible outcomes, all of them verified:

| Outcome | When | Card behavior |
|---|---|---|
| **Full** | Verify passed within N attempts | Normal render |
| **Conservative** | Fell back to strict mode | Card renders with a small "auto-verified ⚠️ conservative mode" badge, drawer lists what was excluded |
| **Empty** | Conservative produced nothing | Card omitted entirely (`show: false`); inputs/extract still stored for debugging |

The extracted intermediate JSON is stored in `insights.inputs.risk_news_extract` so "why did the card say that?" (or "why didn't it?") is debuggable without re-running Claude. Each verify attempt's critique is also stored under `insights.inputs.risk_news_attempts` for the same reason.

---

## 7. Endpoints

All `@require_auth`, bearer token same as existing routes.

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/insights/dashboard` | Cached card payloads (lazy-generates on miss) |
| `POST` | `/insights/refresh`   | Force regenerate today's doc (like `?refresh=true` on `/analyze`) |
| `GET`  | `/benchmark/<symbol>` | Timeseries for chart overlay, supports `?from=&to=` |
| `GET`  | `/insights/risk-news/<symbol>` | Drill-down for one held name (drawer detail) |

---

## 8. Failure modes

| Failure | Behavior |
|---|---|
| FMP `stock_news` fails | Card 3 renders using analyzer signals only; sources footer notes "news unavailable" |
| FMP benchmark prices fail | Card 1 hides; chart overlay button greyed out |
| Anthropic call fails | Card renders with stats only, `prose: null`; frontend shows generic headline |
| Verification can't ground full output | Falls back to conservative mode (strict citation rule); card renders with `⚠ conservative` badge |
| Conservative mode also produces nothing | Card omitted entirely (`show: false`); never ships un-grounded prose |
| User holds nothing | `cards: []`, row doesn't render |
| Cron misses (weekend/outage) | First dashboard load lazily regenerates (~10s) |
| Two workers race the cron | `meta.insert_one("insights_tick_<date>")` ensures only one runs |

---

## 9. Out of scope for v1

- Card dismiss / snooze ("hide for 7 days") — revisit if cards become noisy.
- Pre-warm scheduler for weekends. Lazy is fine to start.
- User-tunable thresholds (e.g., "warn me at 20% concentration not 25%"). Hard-code first, configure later if needed.
- Insights history view (trends in concentration over time). The collection supports it but no UI in v1.
- Multi-user support. App is single-user; one doc per day is sufficient.

---

## 10. Open items

None blocking — locked design as of 2026-05-17. Next step is implementation.
