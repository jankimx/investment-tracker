"""
Dashboard insights engine.

Computes the daily-cached "what's notable about your portfolio" payload that
drives the CTA card row on the dashboard. See INSIGHTS_DESIGN.md.

Pure-math, card builders, and per-card generators live here. Claude prose
calls live in claude_synthesis.py. App.py owns DB I/O, cache collections,
and the per-card workers.

Per-card caching: each card type is keyed independently. CARD_VERSIONS lets
us bump a single card's version to force just that card to regenerate while
leaving the others alone.
"""

from datetime import datetime


# -- Card registry -------------------------------------------------
# Bump a value here to force just that card to regenerate on next read.
# Used by app.py to invalidate per-card caches.
CARD_VERSIONS = {
    "concentration": 2,   # v2: empty-state card when below threshold (no more silent hide)
    "benchmark":     3,   # v3: headline format "pp" -> "%" (user-friendly)
    "risk_news":     3,   # v3: FMP news endpoint migrated v3/stock_news -> stable/news/stock
}

CARD_IDS = list(CARD_VERSIONS.keys())

# Surfaced in the API response so the frontend can label loading
# placeholders with something specific instead of a generic "loading...".
CARD_DISPLAY_NAMES = {
    "concentration": "Concentration",
    "benchmark":     "Performance vs benchmark",
    "risk_news":     "Risk & news",
}

CARD_LOADING_MESSAGES = {
    "concentration": "Analyzing portfolio concentration…",
    "benchmark":     "Comparing against benchmarks…",
    "risk_news":     "Reviewing news and analyzer signals…",
}

# Empty-state copy shown when a card has been generated but didn't surface
# anything actionable. Keeps the slot visible (vs hiding silently) so the
# user can tell the difference between "we checked and there's nothing" and
# "the fetch failed" or "deploy added a new card we haven't built yet."
CARD_EMPTY_STATE = {
    "concentration": {
        "headline": "Portfolio is well-diversified",
        "subtitle": "No single name above 25% of total value, top 3 below 50%.",
    },
    "benchmark": {
        "headline": "Tracking SPY closely",
        "subtitle": "Portfolio return is within 2pp of the default benchmark this period.",
    },
    "risk_news": {
        "headline": "No notable activity",
        "subtitle": "No critical news or analyzer signals for your current holdings.",
    },
}


def _empty_card(card_id, subtitle_override=None):
    """Build the muted empty-state payload for a card."""
    base = CARD_EMPTY_STATE.get(card_id, {})
    return {
        "id":       card_id,
        "show":     True,
        "empty":    True,           # frontend renders no CTA button, neutral border
        "severity": "info",
        "headline": base.get("headline", "Nothing to show"),
        "subtitle": subtitle_override or base.get("subtitle", ""),
    }


# -- Thresholds ----------------------------------------------------
# Hard-coded for v1. INSIGHTS_DESIGN.md §9 lists user-tunable thresholds
# as out-of-scope.
CONCENTRATION_TOP_1_THRESHOLD  = 25.0  # %
CONCENTRATION_TOP_3_THRESHOLD  = 50.0  # %
CONCENTRATION_CRITICAL_TOP_1   = 40.0  # %
CONCENTRATION_CRITICAL_TOP_3   = 70.0  # %

BENCHMARK_DELTA_THRESHOLD_PP   = 2.0   # show the card when |portfolio - benchmark| >= 2pp
BENCHMARK_CRITICAL_DELTA_PP    = 5.0   # critical severity at >= 5pp
DEFAULT_BENCHMARK              = "SPY"
AVAILABLE_BENCHMARKS           = ["SPY", "QQQ", "VTI"]

RISK_VERIFY_MAX_ATTEMPTS       = 5
RISK_MIN_ITEMS_FOR_CARD        = 1     # show card with even one notable item
RISK_CRITICAL_THRESHOLD        = 1     # ≥1 critical-severity item bumps card to critical

# Title keywords used by the conservative deterministic fallback. If Claude
# verification keeps failing, we fall back to extracting items whose source
# titles contain one of these terms — output is guaranteed citable to source.
RISK_KEYWORDS_DOWNSIDE = [
    "downgrade", "downgrades", "miss", "missed",
    "probe", "investigation", "lawsuit", "fraud",
    "warning", "recall", "scandal", "suspended",
    "guidance cut", "guidance lowered",
]
RISK_KEYWORDS_UPSIDE = [
    "upgrade", "upgrades", "beats", "exceeds", "tops",
    "buyback", "dividend hike", "raises guidance", "raised guidance",
    "approves", "expansion",
]


# -- Concentration card -------------------------------------------
def compute_concentration(positions):
    """Pure-math summary of single-name and platform concentration.

    Args:
        positions: list of position dicts from derive_all_positions().
                   Each must have at least: stock, platform, value.

    Returns the stats dict consumed by build_concentration_card(), or None
    if the portfolio has nothing to summarize.
    """
    rows = [p for p in positions if (p.get("value") or 0) > 0]
    total = sum(p["value"] for p in rows)
    if not rows or total <= 0:
        return None

    # Per-position rows (one entry per (platform, stock)), sorted by value desc.
    top_holdings = sorted(
        ({
            "symbol":     p["stock"],
            "platform":   p["platform"],
            "value":      round(p["value"], 2),
            "weight_pct": round(p["value"] / total * 100, 2),
        } for p in rows),
        key=lambda r: r["value"], reverse=True,
    )

    # Stock-level weights (same ticker across multiple platforms collapses).
    by_stock = {}
    for p in rows:
        by_stock[p["stock"]] = by_stock.get(p["stock"], 0) + p["value"]
    stock_weights = sorted(
        ({"symbol": s, "value": round(v, 2),
          "weight_pct": round(v / total * 100, 2)}
         for s, v in by_stock.items()),
        key=lambda r: r["value"], reverse=True,
    )

    top_1 = stock_weights[0]["weight_pct"] if stock_weights else 0.0
    top_3 = round(sum(r["weight_pct"] for r in stock_weights[:3]), 2)
    top_5 = round(sum(r["weight_pct"] for r in stock_weights[:5]), 2)
    # Herfindahl-Hirschman Index, normalized 0..1. 1 = single position.
    hhi = round(sum((r["weight_pct"] / 100) ** 2 for r in stock_weights), 4)

    by_platform = {}
    for p in rows:
        by_platform[p["platform"]] = by_platform.get(p["platform"], 0) + p["value"]
    platforms = sorted(
        ({"platform": k, "value": round(v, 2),
          "weight_pct": round(v / total * 100, 2)}
         for k, v in by_platform.items()),
        key=lambda r: r["value"], reverse=True,
    )

    return {
        "total_value":   round(total, 2),
        "top_holdings":  top_holdings,
        "stock_weights": stock_weights,
        "concentration": {
            "top_1_pct": round(top_1, 2),
            "top_3_pct": top_3,
            "top_5_pct": top_5,
            "hhi":       hhi,
        },
        "platforms": platforms,
    }


def build_concentration_card(stats, prose=None):
    """Turn concentration stats into the card payload that lives in
    insights.cards[]. Returns None when no threshold is crossed (card
    should not render)."""
    if not stats:
        return None
    c = stats["concentration"]
    top_1 = c["top_1_pct"]
    top_3 = c["top_3_pct"]

    if top_1 < CONCENTRATION_TOP_1_THRESHOLD and top_3 < CONCENTRATION_TOP_3_THRESHOLD:
        return None

    severity = ("critical" if (top_1 >= CONCENTRATION_CRITICAL_TOP_1
                               or top_3 >= CONCENTRATION_CRITICAL_TOP_3)
                else "warn")

    top_stock = stats["stock_weights"][0]
    if top_1 >= CONCENTRATION_TOP_1_THRESHOLD:
        headline = f"{top_stock['symbol']} is {top_1:.0f}% of your portfolio"
        trigger  = {"rule": "top_1_pct_gt",
                    "threshold": CONCENTRATION_TOP_1_THRESHOLD, "actual": top_1}
    else:
        names    = ", ".join(s["symbol"] for s in stats["stock_weights"][:3])
        headline = f"Top 3 ({names}) are {top_3:.0f}% of your portfolio"
        trigger  = {"rule": "top_3_pct_gt",
                    "threshold": CONCENTRATION_TOP_3_THRESHOLD, "actual": top_3}

    return {
        "id":        "concentration",
        "show":      True,
        "severity":  severity,
        "headline":  headline,
        "cta_label": "View allocation",
        "prose":     prose,
        "detail": {
            "total_value":   stats["total_value"],
            "top_holdings":  stats["top_holdings"],
            "stock_weights": stats["stock_weights"],
            "concentration": stats["concentration"],
            "platforms":     stats["platforms"],
            "trigger":       trigger,
        },
    }


# -- Benchmark card -----------------------------------------------
def compute_benchmark_comparison(portfolio_totals_by_date, benchmark_closes_by_symbol):
    """Compute portfolio-vs-benchmark return over the longest window the
    portfolio data supports.

    Args:
        portfolio_totals_by_date: {YYYY-MM-DD: total_portfolio_value}.
                                  Caller produces this (e.g., from
                                  derive_chart_series rolled up across positions).
        benchmark_closes_by_symbol: {symbol: {YYYY-MM-DD: close_price}}.
                                    Keys are benchmark tickers (SPY/QQQ/VTI).

    Returns the comparison stats dict, or None if there isn't enough data
    to compute even one benchmark.
    """
    dates = sorted(d for d, v in portfolio_totals_by_date.items() if v and v > 0)
    if len(dates) < 2:
        return None

    actual_start = dates[0]
    actual_end   = dates[-1]
    p_start      = portfolio_totals_by_date[actual_start]
    p_end        = portfolio_totals_by_date[actual_end]
    if p_start <= 0:
        return None
    portfolio_pct = (p_end / p_start - 1) * 100

    comparisons = []
    for symbol in AVAILABLE_BENCHMARKS:
        closes = benchmark_closes_by_symbol.get(symbol, {})
        if not closes:
            continue
        # For the start: take the earliest benchmark date >= portfolio's start
        # (handles holidays/weekends and backfill gaps). For the end: take
        # the latest benchmark date <= portfolio's end. Both must exist.
        bench_dates = sorted(closes.keys())
        starts = [d for d in bench_dates if d >= actual_start]
        ends   = [d for d in bench_dates if d <= actual_end]
        if not starts or not ends:
            continue
        bench_start = closes[starts[0]]
        bench_end   = closes[ends[-1]]
        if bench_start <= 0:
            continue
        benchmark_pct = (bench_end / bench_start - 1) * 100
        comparisons.append({
            "benchmark":      symbol,
            "portfolio_pct":  round(portfolio_pct, 2),
            "benchmark_pct":  round(benchmark_pct, 2),
            "delta_pp":       round(portfolio_pct - benchmark_pct, 2),
        })

    if not comparisons:
        return None

    return {
        "period": {
            "from":  actual_start,
            "to":    actual_end,
            "label": "YTD",
        },
        "portfolio_start_value": round(p_start, 2),
        "portfolio_end_value":   round(p_end, 2),
        "default_benchmark":     DEFAULT_BENCHMARK,
        "available_benchmarks":  AVAILABLE_BENCHMARKS,
        "comparisons":           comparisons,
    }


def build_benchmark_card(stats, prose=None):
    """Turn benchmark stats into the card payload. Returns None when no
    benchmark crosses the delta threshold."""
    if not stats or not stats.get("comparisons"):
        return None

    default = stats["default_benchmark"]
    default_cmp = next(
        (c for c in stats["comparisons"] if c["benchmark"] == default),
        stats["comparisons"][0],
    )
    delta = default_cmp["delta_pp"]
    abs_delta = abs(delta)

    if abs_delta < BENCHMARK_DELTA_THRESHOLD_PP:
        return None

    severity = "critical" if abs_delta >= BENCHMARK_CRITICAL_DELTA_PP else "warn" if delta < 0 else "info"

    direction = "Trailing" if delta < 0 else "Beating"
    headline  = f"{direction} {default_cmp['benchmark']} by {abs_delta:.1f}% YTD"

    return {
        "id":        "benchmark",
        "show":      True,
        "severity":  severity,
        "headline":  headline,
        "cta_label": "Compare on chart",
        "prose":     prose,
        "detail": {
            "period":               stats["period"],
            "default_benchmark":    stats["default_benchmark"],
            "available_benchmarks": stats["available_benchmarks"],
            "portfolio_start_value": stats["portfolio_start_value"],
            "portfolio_end_value":   stats["portfolio_end_value"],
            "comparisons":          stats["comparisons"],
            "trigger": {
                "rule":      "abs_delta_pp_gte",
                "threshold": BENCHMARK_DELTA_THRESHOLD_PP,
                "actual":    abs_delta,
            },
        },
    }


# -- Risk / news card ---------------------------------------------
def collect_risk_inputs(positions, fetch_news_fn, get_analysis_fn):
    """Gather raw news + analyzer signals for held symbols. Returns a dict
    structured for downstream Claude prompting:

        {
          "by_symbol": {
            "AAPL": {"news": [...], "signals": [...]},
            ...
          },
          "sources": {source_id: source_dict, ...}   # citation lookup
        }

    Args:
        positions:        derived positions list.
        fetch_news_fn:    callable(symbol) -> list of news item dicts.
                          Each item must have id, title, url, published.
        get_analysis_fn:  callable(symbol) -> cached analysis doc or None.
    """
    symbols = sorted({
        p["stock"].upper() for p in positions
        if (p.get("value") or 0) > 0 and p["stock"].upper() != "TOTAL"
    })

    by_symbol = {}
    sources   = {}

    for sym in symbols:
        try:
            news_items = fetch_news_fn(sym) or []
        except Exception as e:
            print(f"[Risk] news fetch failed for {sym}: {e}")
            news_items = []

        try:
            analysis = get_analysis_fn(sym)
        except Exception as e:
            print(f"[Risk] analysis lookup failed for {sym}: {e}")
            analysis = None

        signals = _extract_analyzer_signals(sym, analysis)

        if not news_items and not signals:
            continue

        by_symbol[sym] = {"news": news_items, "signals": signals}
        for n in news_items:
            if n.get("id"):
                sources[n["id"]] = {"type": "news", "symbol": sym, **n}
        for s in signals:
            if s.get("id"):
                sources[s["id"]] = {"type": "analyzer", **s}

    return {"by_symbol": by_symbol, "sources": sources}


def _extract_analyzer_signals(symbol, analysis):
    """Turn a cached analysis doc into a flat list of signals usable as
    citation sources. Drops anything not high-signal (neutral insider, low
    value-trap risk, etc.) so we don't dilute the risk card with noise."""
    if not analysis:
        return []
    scores = analysis.get("scores") or {}
    out = []

    for i, flag in enumerate(scores.get("red_flags") or []):
        title = flag.get("title")
        if not title:
            continue
        out.append({
            "id":       f"analyzer_{symbol}_redflag_{i}",
            "symbol":   symbol,
            "kind":     "red_flag",
            "severity": flag.get("severity", "medium"),
            "title":    title,
            "detail":   flag.get("detail") or flag.get("why_it_matters") or "",
        })

    trap = scores.get("value_trap") or {}
    if trap.get("risk_level") in ("high", "critical"):
        out.append({
            "id":       f"analyzer_{symbol}_valuetrap",
            "symbol":   symbol,
            "kind":     "value_trap",
            "severity": "high" if trap.get("risk_level") == "high" else "critical",
            "title":    f"Elevated value-trap risk ({trap.get('risk_level')})",
            "detail":   trap.get("detail") or trap.get("reasoning") or "",
        })

    insider = scores.get("insider") or {}
    insider_signal = insider.get("signal")
    if insider_signal in ("cluster_buying", "cluster_selling", "buying", "selling"):
        out.append({
            "id":       f"analyzer_{symbol}_insider",
            "symbol":   symbol,
            "kind":     "insider",
            "severity": "medium",
            "title":    f"Notable insider activity: {insider_signal.replace('_', ' ')}",
            "detail":   insider.get("detail") or "",
        })

    return out


def build_risk_card(verified, prose=None):
    """Turn verified risk items + verification metadata into the card.
    Returns None when there are zero items to render."""
    items = verified.get("items") or []
    if len(items) < RISK_MIN_ITEMS_FOR_CARD:
        return None

    by_severity = {"critical": 0, "warn": 0, "info": 0}
    for it in items:
        sev = it.get("severity") if it.get("severity") in by_severity else "info"
        by_severity[sev] += 1

    if by_severity["critical"] >= RISK_CRITICAL_THRESHOLD:
        severity = "critical"
    elif by_severity["warn"] > 0:
        severity = "warn"
    else:
        severity = "info"

    n_symbols = len({it.get("symbol") for it in items if it.get("symbol")})
    headline = (
        f"{n_symbols} holding{'s' if n_symbols != 1 else ''} "
        f"with notable activity"
    )

    return {
        "id":        "risk_news",
        "show":      True,
        "severity":  severity,
        "headline":  headline,
        "cta_label": "View summary",
        "prose":     prose,
        "verification": verified.get("verification"),
        "detail": {
            "items":   items,
            "trigger": {
                "rule":      "items_gte",
                "threshold": RISK_MIN_ITEMS_FOR_CARD,
                "actual":    len(items),
            },
        },
    }


def conservative_risk_extract(inputs):
    """Deterministic fallback. Pulls items whose source content is trivially
    citable (news titles matching a keyword whitelist; analyzer signals
    verbatim). Guaranteed to verify because every output claim is a direct
    pull from the source text."""
    items = []
    for symbol, blob in (inputs.get("by_symbol") or {}).items():
        for n in (blob.get("news") or []):
            title = (n.get("title") or "").lower()
            direction = None
            if any(k in title for k in RISK_KEYWORDS_DOWNSIDE):
                direction = "downside"
            elif any(k in title for k in RISK_KEYWORDS_UPSIDE):
                direction = "upside"
            if direction is None:
                continue
            items.append({
                "symbol":     symbol,
                "direction":  direction,
                "severity":   "warn" if direction == "downside" else "info",
                "summary":    n.get("title"),
                "source_ids": [n["id"]],
            })
        for s in (blob.get("signals") or []):
            kind = s.get("kind")
            direction = "downside" if kind in ("red_flag", "value_trap") else "info"
            severity  = s.get("severity") or ("warn" if direction == "downside" else "info")
            # collapse analyzer-severity vocabulary into the card's tiers
            severity  = {"high": "critical", "medium": "warn", "low": "info"}.get(severity, severity)
            if severity not in ("critical", "warn", "info"):
                severity = "warn"
            items.append({
                "symbol":     s["symbol"],
                "direction":  direction if direction in ("upside", "downside") else "downside",
                "severity":   severity,
                "summary":    s.get("title"),
                "source_ids": [s["id"]],
            })
    return items


def _resolve_sources(items, sources):
    """Inline the full source dicts each item cites so the frontend can
    render links + attribution without a second API round-trip. Unknown
    source_ids are dropped silently."""
    resolved = []
    for it in items:
        out = dict(it)
        out_sources = []
        for sid in (it.get("source_ids") or []):
            src = sources.get(sid)
            if not src:
                continue
            out_sources.append({
                "id":        sid,
                "type":      src.get("type"),
                "title":     src.get("title"),
                "url":       src.get("url"),         # None for analyzer sources
                "published": src.get("published"),
                "site":      src.get("site"),
                "kind":      src.get("kind"),        # analyzer-specific
                "detail":    src.get("detail"),      # analyzer-specific
            })
        out["sources"] = out_sources
        resolved.append(out)
    return resolved


def synthesize_risk_card_verified(inputs, extract_fn, verify_fn, synthesize_fn,
                                  max_attempts=RISK_VERIFY_MAX_ATTEMPTS):
    """Run the bounded write -> verify loop. On failure after max_attempts,
    fall back to deterministic conservative extraction so we still ship
    verifiable output. Output is ALWAYS verified.

    Args:
        inputs:         dict from collect_risk_inputs(); keys "by_symbol" + "sources".
        extract_fn:     callable(inputs, prior_critique=None) -> list of items.
        verify_fn:      callable(items, sources) -> {grounded, unsupported_claims}.
        synthesize_fn:  callable(items) -> str prose.

    Returns dict with items, prose, verification meta — or None if there
    are no inputs to summarize at all.
    """
    if not inputs.get("by_symbol"):
        return None

    last_critique  = None
    claude_calls   = 0

    for attempt in range(max_attempts):
        try:
            items = extract_fn(inputs, prior_critique=last_critique)
            claude_calls += 1
            verification = verify_fn(items, inputs["sources"])
            claude_calls += 1
            if verification.get("grounded"):
                prose = None
                try:
                    prose = synthesize_fn(items)
                    claude_calls += 1
                except Exception as e:
                    print(f"[Risk] Synthesize failed (verified path): {e}")
                return {
                    "items":        _resolve_sources(items, inputs["sources"]),
                    "prose":        prose,
                    "claude_calls": claude_calls,
                    "verification": {
                        "passes":          attempt + 1,
                        "mode":            "verified",
                        "claims_total":    len(items),
                        "claims_grounded": len(items),
                        "warnings":        [],
                    },
                }
            last_critique = verification.get("unsupported_claims") or []
            print(f"[Risk] Attempt {attempt+1} failed verification "
                  f"({len(last_critique)} issues)")
        except Exception as e:
            print(f"[Risk] Attempt {attempt+1} errored: {type(e).__name__}: {e}")
            last_critique = [f"Previous attempt errored: {e}"]

    # Conservative deterministic fallback — guaranteed citable.
    print(f"[Risk] Falling back to conservative mode after {max_attempts} attempts")
    items = conservative_risk_extract(inputs)
    prose = None
    if items:
        try:
            prose = synthesize_fn(items)
            claude_calls += 1
        except Exception as e:
            print(f"[Risk] Conservative synthesize failed: {e}")

    return {
        "items":        _resolve_sources(items, inputs["sources"]),
        "prose":        prose,
        "claude_calls": claude_calls,
        "verification": {
            "passes":          max_attempts,
            "mode":            "conservative",
            "claims_total":    len(items),
            "claims_grounded": len(items),
            "warnings": [
                "Auto-verification did not pass after retries; fell back to "
                "conservative title-only extraction."
            ],
        },
    }


# -- Per-card generators -----------------------------------------
# Each returns (payload_or_None, claude_calls). payload is the card dict
# if it would render, or None if it didn't trigger (threshold not met /
# no input data). Callers cache the result under the card's version so a
# null payload also counts as "generated, just not shown" — avoiding
# redundant re-runs within the day.

def generate_concentration_card(positions, prose_fn=None):
    """Returns (payload, claude_calls). Always returns a payload when the
    portfolio has any holdings — either an actionable card or a muted
    empty-state card. Only returns (None, 0) when there's literally no
    portfolio to evaluate."""
    stats = compute_concentration(positions)
    if not stats:
        return None, 0
    card = build_concentration_card(stats, prose=None)
    if not card:
        return _empty_card("concentration"), 0
    claude_calls = 0
    if prose_fn is not None:
        try:
            card["prose"] = prose_fn("concentration", stats)
            claude_calls += 1
        except Exception as e:
            print(f"[Insights] concentration prose failed: {type(e).__name__}: {e}")
    return card, claude_calls


def generate_benchmark_card(prose_fn=None, benchmark_fn=None, has_portfolio=True):
    """Always returns a payload when there's a portfolio with benchmark
    coverage. Falls back to empty-state when within threshold of the
    benchmark. Returns (None, 0) only when we can't even compute a
    comparison (no portfolio history or all benchmark fetches failed)."""
    if benchmark_fn is None:
        return None, 0
    try:
        portfolio_totals, benchmark_closes = benchmark_fn()
    except Exception as e:
        print(f"[Insights] benchmark data fetch failed: {type(e).__name__}: {e}")
        return None, 0
    stats = compute_benchmark_comparison(portfolio_totals, benchmark_closes)
    if not stats:
        # no portfolio history vs benchmark — leave card off entirely
        return None, 0
    card = build_benchmark_card(stats, prose=None)
    if not card:
        # within threshold of benchmark — show muted empty state with the
        # actual delta in the subtitle so the user knows by how much.
        default = stats.get("default_benchmark", "SPY")
        default_cmp = next(
            (c for c in stats["comparisons"] if c["benchmark"] == default),
            stats["comparisons"][0],
        )
        delta = default_cmp["delta_pp"]
        direction = "ahead of" if delta > 0 else "behind"
        subtitle = (f"Portfolio is {abs(delta):.1f}% {direction} {default} "
                    f"this period — within the {BENCHMARK_DELTA_THRESHOLD_PP}% threshold.")
        return _empty_card("benchmark", subtitle_override=subtitle), 0
    claude_calls = 0
    if prose_fn is not None:
        try:
            card["prose"] = prose_fn("benchmark", stats)
            claude_calls += 1
        except Exception as e:
            print(f"[Insights] benchmark prose failed: {type(e).__name__}: {e}")
    return card, claude_calls


def generate_risk_news_card(risk_fn=None, has_holdings=True):
    """Always returns a payload when the user has holdings — either an
    actionable card with items or a muted empty-state card. Only returns
    (None, 0) when there are no holdings at all."""
    if not has_holdings:
        return None, 0
    if risk_fn is None:
        # Claude not configured; still show empty state so the user knows
        # we tried (and what's missing).
        return _empty_card(
            "risk_news",
            subtitle_override="AI synthesis is not configured (ANTHROPIC_API_KEY missing).",
        ), 0
    try:
        verified = risk_fn()
    except Exception as e:
        print(f"[Insights] risk pipeline failed: {type(e).__name__}: {e}")
        return _empty_card(
            "risk_news",
            subtitle_override="Could not fetch news or analyzer signals right now.",
        ), 0
    if not verified or not verified.get("items"):
        return _empty_card("risk_news"), (verified or {}).get("claude_calls", 0)
    card = build_risk_card(verified, prose=verified.get("prose"))
    if not card:
        return _empty_card("risk_news"), verified.get("claude_calls", 0)
    return card, verified.get("claude_calls", 0)


CARD_GENERATORS = {
    "concentration": generate_concentration_card,
    "benchmark":     generate_benchmark_card,
    "risk_news":     generate_risk_news_card,
}
