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

### 3.1 Per-card collections

One **collection per card type**, each keyed by date. Replaces the original "one daily doc per day" design — see §3.4 for why.

| Collection | Card |
|---|---|
| `insights_concentration` | Concentration |
| `insights_benchmark`     | Benchmark    |
| `insights_risk_news`     | Risk / news  |

Every doc has the same envelope:

```jsonc
{
  "_id":          "2026-05-17",      // date string, primary key
  "version":      1,                 // matches insights.CARD_VERSIONS[card_id] at generation time
  "generated_at": "2026-05-17T20:32:11Z",
  "duration_ms":  4821,
  "claude_calls": 1,
  "trigger":      "cron",            // "cron" | "lazy" | "manual"
  "payload":      { ... }            // the card dict (§3.3) — OR null if it didn't trigger
}
```

`payload: null` is a valid cached result — it means "we evaluated this card today and there's nothing to render." The frontend omits it; the worker doesn't redo it within the day. Bump `insights.CARD_VERSIONS[card_id]` to force just that card to regen.

### 3.2 Card payloads

The body of `payload` differs per card type but follows the original §3.2 shapes (concentration / benchmark / risk_news). The wrapper is the same — only `payload` varies.

```jsonc
// historical: when the design was "one doc with all cards", this was the
// cards[] array body. Now the same body is stored per-card under
// insights_<card_id>.payload. See §3.3.
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
// Each per-card collection — primary _id is implicit, secondary for debugging.
["insights_concentration", "insights_benchmark", "insights_risk_news"].forEach(c => {
  db[c].createIndex({ generated_at: -1 })       // "show me last N days"
});
db.news.createIndex({ symbol: 1, date: -1 })    // drill-down lookups
```

### 3.5 Why per-card collections (rather than one daily doc)

The original design stored all three cards under a single `insights[YYYY-MM-DD]` document. After Phase 3 shipped we hit the predictable failure: deploying a new card type didn't update yesterday's-still-cached doc, so the new card never appeared until the next day's cron. Switching to one collection per card type fixes this:

- **Independent invalidation.** Bump `CARD_VERSIONS[card_id]` in code → only that card regenerates on next read; the others stay cached.
- **Independent generation.** Per-card locks let the three cards generate **concurrently** on a cold start. Total wait shrinks from "sum of all card times" to "slowest single card."
- **Independent failure.** A Claude/FMP failure on one card doesn't block the others from rendering.
- **Independent loading UX.** Each card slot shows its own labeled placeholder while it generates — see §4.1.

The deprecated `insights` collection is no longer read or written. Old data can be dropped manually; the code ignores it.

---

## 4. Read path

`GET /insights/dashboard` returns one entry per expected card. Each entry has its own `status`. Cards that have been generated but didn't trigger (payload=null) are omitted entirely.

```jsonc
{
  "as_of": "2026-05-17T20:32:11Z",          // newest generated_at across the cached cards
  "cards": [
    { "id": "concentration", "status": "ready", "show": true, "severity": "warn", "headline": "...", ... },
    { "id": "benchmark",     "status": "generating", "display_name": "Performance vs benchmark",
      "loading_message": "Comparing against benchmarks…" },
    // risk_news was generated and didn't trigger — omitted
  ],
  "generating": ["benchmark"]                // convenience: card_ids still being built
}
```

```
GET /insights/dashboard
  for each card_id in CARD_VERSIONS:
    look up insights_<card_id>[today]
      ├─ hit + cached version == current version → emit { status: "ready", ...payload }
      │                                              (omit if payload is null / show is false)
      └─ miss or version stale → claim per-card lock, spawn worker, emit { status: "generating", ... }
```

No business logic on the client — the frontend renders whatever the server returns, in the order returned.

### 4.1 Frontend polling + per-card placeholders

For each `status: "generating"` entry the frontend renders a labeled placeholder card in that slot, with the server-supplied `display_name` and `loading_message`. Cached cards render their full content immediately. Polling continues only while `generating` is non-empty, with **exponential backoff** (1, 2, 4, 8, 16, 32s, capped at 60s) — backoff resets to 1s whenever the generating set changes so we catch transitions promptly.

```
┌──────────────────────────────┐  ┌──────────────────────────────┐  ┌──────────────────────────────┐
│ Concentration                │  │ Performance vs benchmark     │  │ Risk & news                  │
│ AAPL is 32% of your portfolio│  │ Comparing against           │  │ 2 holdings with notable     │
│ ...                          │  │ benchmarks...                │  │ activity                     │
│ [View allocation]            │  │                              │  │ [View summary]               │
└──────────────────────────────┘  └──────────────────────────────┘  └──────────────────────────────┘
       ↑ ready (cached)                  ↑ still generating                ↑ ready (cached)
```

Cards sort by severity (`critical → warn → info`); placeholders sort to the end (no severity) and slot into their proper position once the real payload arrives.

### 4.2 Concurrent-generation protection

Per-card locks in the existing `meta` collection. Each lock document carries an explicit `expires_at` ~180s in the future (generous because the risk card can run a multi-pass Claude loop), and a TTL index on that field auto-deletes the doc once it's in the past.

```jsonc
{
  "_id":        "insights_lock_benchmark_2026-05-17",
  "started_at": ISODate("2026-05-17T22:15:01Z"),
  "expires_at": ISODate("2026-05-17T22:18:01Z"),
  "pid":        12345,
  "card_id":    "benchmark"
}
```

```js
db.meta.createIndex({ expires_at: 1 }, { expireAfterSeconds: 0 })
```

Why `expireAfterSeconds: 0` on `expires_at` instead of a per-collection TTL? MongoDB's TTL applies to every doc with the indexed field as a date — existing meta docs (`refresh_tick_*`, `schema_migration_v2`, etc.) don't have `expires_at`, so the TTL ignores them. Cleaner than `_id` regex filtering (which `partialFilterExpression` doesn't support anyway).

Per-card locks mean the three cards generate **concurrently** on a cold start, so the total wait equals the slowest single card. First request to claim each lock spawns its worker; subsequent requests see the lock (`DuplicateKeyError`) and return `generating` without spawning a duplicate. When a worker finishes it deletes its own lock. Crashed workers are cleaned up automatically by TTL.

Each card also has an in-process `threading.Lock` (lazy-init dict keyed by `card_id`) so a single worker process can't accidentally spawn two threads for the same card. The Mongo lock handles cross-worker dedup; the in-process lock handles same-worker races.

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
    today = today_str()
    for card_id in CARD_IDS:
        # Cron clears any cached lock so it can re-claim and force regen
        # (daily refresh deliberately invalidates yesterday's caches).
        _release_card_lock(today, card_id)
        _spawn_card_generation(today, card_id, trigger="cron")
```

### 5.2 Lazy fallback

If the cron didn't run (weekend, holiday, outage, fresh deploy), the first dashboard load of the day calls `_spawn_card_generation` for each stale card. Same worker, same lock pattern — the only difference is `trigger="lazy"` for telemetry.

**Trade-off the user accepted:** weekends and holidays trigger generation on first load. Per-card parallelism keeps the wait to the slowest single card (~5-30s for risk_news, much less for the others).

### 5.3 The per-card worker

```python
def _generate_one_card_and_persist(today, card_id, trigger):
    # acquired the in-process lock for this card
    payload, claude_calls = CARD_GENERATORS[card_id](positions, ...closures...)
    CARD_COLLECTIONS[card_id].replace_one(
        {"_id": today},
        {
            "_id":          today,
            "version":      CARD_VERSIONS[card_id],
            "generated_at": ...,
            "duration_ms":  ...,
            "claude_calls": claude_calls,
            "trigger":      trigger,
            "payload":      payload,    # may be None (computed but didn't trigger)
        },
        upsert=True,
    )
```

Generators (`generate_concentration_card`, `generate_benchmark_card`, `generate_risk_news_card`) live in `insights.py` and return `(payload, claude_calls)`. App.py wires in the Claude prose/extract/verify/synthesize callables and the FMP-backed `benchmark_fn` / `fetch_news` closures.

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
| Cron misses (weekend/outage) | First dashboard load spawns per-card lazy regeneration |
| Two workers race a card | Per-card `meta.insert_one("insights_lock_<card>_<date>")` ensures only one runs that card |
| Single card fails | Other cards still render; the failed slot stays as a "generating" placeholder until next retry |
| Deploy adds new card | New card auto-regens on next read (its cache is missing); existing cards keep serving from cache |

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
