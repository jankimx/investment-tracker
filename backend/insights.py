"""
Dashboard insights engine.

Computes the daily-cached "what's notable about your portfolio" payload that
drives the CTA card row on the dashboard. See INSIGHTS_DESIGN.md for the
full design.

Pure-math + orchestration live here. Claude prose calls live in
claude_synthesis.py so the AI-prompting code stays in one place.

Phase 1 ships the concentration card only. Benchmark and risk/news cards
follow in later phases.
"""

from datetime import datetime


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
    headline  = f"{direction} {default_cmp['benchmark']} by {abs_delta:.1f}pp YTD"

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


# -- Orchestrator -------------------------------------------------
def generate_insights(positions, prose_fn=None, benchmark_fn=None, trigger="lazy"):
    """Assemble today's insights document.

    Args:
        positions:     list of derived positions (from derive_all_positions()).
        prose_fn:      optional callable(card_id, stats) -> str.
                       Generates Claude-written prose per card. May raise;
                       on exception we log and ship the card with prose=None.
        benchmark_fn:  optional callable() -> (portfolio_totals_by_date,
                       benchmark_closes_by_symbol). Caller provides this
                       (it needs DB / FMP access). May raise; on exception
                       the benchmark card is skipped.
        trigger:       "lazy", "cron", or "manual" — recorded for telemetry.

    Returns the document body to be stored in the `insights` collection
    (caller wraps with _id = today).
    """
    started      = datetime.utcnow()
    claude_calls = 0
    cards        = []

    def _attach(card_id, stats, builder):
        """Build the card without prose first (cheap threshold check). Only
        if it would render do we burn a Claude call for the prose."""
        nonlocal claude_calls
        card = builder(stats, prose=None)
        if not card:
            return
        if prose_fn is not None:
            try:
                card["prose"] = prose_fn(card_id, stats)
                claude_calls += 1
            except Exception as e:
                print(f"[Insights] {card_id} prose failed: {type(e).__name__}: {e}")
        cards.append(card)

    concentration_stats = compute_concentration(positions)
    if concentration_stats:
        _attach("concentration", concentration_stats, build_concentration_card)

    if benchmark_fn is not None:
        try:
            portfolio_totals, benchmark_closes = benchmark_fn()
            benchmark_stats = compute_benchmark_comparison(portfolio_totals, benchmark_closes)
        except Exception as e:
            print(f"[Insights] Benchmark data fetch failed: {type(e).__name__}: {e}")
            benchmark_stats = None
        if benchmark_stats:
            _attach("benchmark", benchmark_stats, build_benchmark_card)

    # Phase 3: risk/news card

    finished = datetime.utcnow()

    return {
        "generated_at": started.isoformat(),
        "generation": {
            "trigger":      trigger,
            "duration_ms":  int((finished - started).total_seconds() * 1000),
            "claude_calls": claude_calls,
        },
        "inputs": {
            "holdings_count": len([p for p in positions if (p.get("value") or 0) > 0]),
        },
        "cards": cards,
    }
