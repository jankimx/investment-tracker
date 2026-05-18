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


# -- Orchestrator -------------------------------------------------
def generate_insights(positions, prose_fn=None, trigger="lazy"):
    """Assemble today's insights document.

    Args:
        positions: list of derived positions (from derive_all_positions()).
        prose_fn:  optional callable(card_id: str, stats: dict) -> str.
                   Generates Claude-written prose per card. May raise; on
                   exception we log and ship the card with prose=None.
        trigger:   "lazy" or "cron". Recorded for telemetry; does not
                   change behavior.

    Returns the document body to be stored in the `insights` collection
    (caller wraps with _id = today).
    """
    started      = datetime.utcnow()
    claude_calls = 0
    cards        = []

    concentration_stats = compute_concentration(positions)
    if concentration_stats:
        prose = None
        if prose_fn is not None:
            try:
                prose = prose_fn("concentration", concentration_stats)
                claude_calls += 1
            except Exception as e:
                print(f"[Insights] Concentration prose failed: {type(e).__name__}: {e}")
        card = build_concentration_card(concentration_stats, prose=prose)
        if card:
            cards.append(card)

    # Phase 2: benchmark card
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
