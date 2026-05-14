"""
Stock Analysis Engine
Fetches data from Financial Modeling Prep and calculates quality/value scores.
All calculations are pure math - no AI involved here.
AI synthesis happens separately in claude_synthesis.py
"""

import os
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

FMP_KEY = os.getenv("FMP_API_KEY", "")
FMP_BASE = "https://financialmodelingprep.com/stable"
FMP_BASE_V3 = "https://financialmodelingprep.com/api/v3"

# -- FMP Data Fetching ---------------------------------------------------------

def fmp_get(endpoint, params=None, base=FMP_BASE):
    """Generic FMP API fetch. Returns parsed JSON or None on error."""
    if not FMP_KEY:
        raise ValueError("FMP_API_KEY not configured")
    p = params or {}
    p["apikey"] = FMP_KEY
    url = base + endpoint + "?" + urllib.parse.urlencode(p)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print(f"[FMP] Error fetching {endpoint}: {e}")
        return None


def fetch_all_data(symbol):
    """
    Fetch all required data for a symbol in parallel.
    Returns dict of raw data or raises on critical failure.
    """
    symbol = symbol.upper().strip()

    def fetch(name, endpoint, params=None, base=FMP_BASE):
        data = fmp_get(endpoint, params, base)
        return name, data

    tasks = [
        ("profile",        f"/profile",                          {"symbol": symbol}),
        ("key_metrics",    f"/key-metrics",                      {"symbol": symbol, "limit": 10}),
        ("ratios",         f"/ratios",                           {"symbol": symbol, "limit": 10}),
        ("income",         f"/income-statement",                 {"symbol": symbol, "limit": 10}),
        ("cashflow",       f"/cash-flow-statement",              {"symbol": symbol, "limit": 10}),
        ("balance",        f"/balance-sheet-statement",          {"symbol": symbol, "limit": 10}),
        ("dcf",            f"/discounted-cash-flow",             {"symbol": symbol}),
        ("price",          f"/quote",                            {"symbol": symbol}),
        ("insider",        f"/insider-trading",                  {"symbol": symbol, "limit": 50}),
        ("owner_earnings", f"/owner-earnings",                   {"symbol": symbol, "limit": 10}, FMP_BASE_V3),
    ]

    results = {}
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {}
        for t in tasks:
            name     = t[0]
            endpoint = t[1]
            params   = t[2]
            base     = t[3] if len(t) > 3 else FMP_BASE
            future   = executor.submit(fetch, name, endpoint, params, base)
            futures[future] = name
        for future in as_completed(futures):
            name, data = future.result()
            results[name] = data

    # Validate critical data exists
    data_status = {k: bool(v) for k, v in results.items()}
    print(f"[Analyzer] Data status for {symbol}: {data_status}")
    if not results.get("profile") or not results.get("income"):
        missing = [k for k, v in data_status.items() if not v]
        raise ValueError(f"No data found for symbol: {symbol}. Missing endpoints: {missing}. This may require an FMP Starter plan.")

    return results


# -- Score Calculations --------------------------------------------------------

def safe_get(data, key, default=None):
    """Safely get a value from a dict."""
    if not data:
        return default
    return data.get(key, default)


def safe_list_get(lst, index=0, key=None, default=None):
    """Safely get item from list, optionally a key from that item."""
    if not lst or not isinstance(lst, list) or len(lst) <= index:
        return default
    item = lst[index]
    if key:
        return item.get(key, default) if isinstance(item, dict) else default
    return item


def pct(value):
    """Convert decimal to percentage if needed."""
    if value is None:
        return None
    return value * 100 if abs(value) < 10 else value


# -- Quality Score Components --------------------------------------------------

def score_roic(key_metrics):
    """
    Score ROIC consistency over up to 10 years.
    Max 20 points.

    Scoring:
    - Average ROIC > 20%: up to 20 pts
    - Consistency (low variance): bonus
    - Declining trend: penalty

    Guardrail: if fewer than 3 years of data, confidence = low
    """
    if not key_metrics:
        return {"score": 0, "max": 20, "confidence": "low",
                "detail": None, "values": [], "note": "No data available"}

    roic_values = []
    for m in key_metrics:
        v = m.get("returnOnInvestedCapital")
        if v is not None:
            roic_values.append(float(v) * 100)  # Convert to percentage

    if not roic_values:
        return {"score": 0, "max": 20, "confidence": "low",
                "detail": None, "values": [], "note": "ROIC data unavailable"}

    avg_roic = sum(roic_values) / len(roic_values)
    years_above_15 = sum(1 for v in roic_values if v >= 15)
    years_above_20 = sum(1 for v in roic_values if v >= 20)
    total_years = len(roic_values)

    # Trend: is ROIC improving or declining?
    if len(roic_values) >= 3:
        recent_avg = sum(roic_values[:3]) / 3
        old_avg = sum(roic_values[-3:]) / 3
        trend = "improving" if recent_avg > old_avg + 2 else \
                "declining" if recent_avg < old_avg - 2 else "stable"
    else:
        trend = "insufficient data"

    # Score calculation
    if avg_roic >= 25:
        base_score = 18
    elif avg_roic >= 20:
        base_score = 16
    elif avg_roic >= 15:
        base_score = 13
    elif avg_roic >= 10:
        base_score = 9
    elif avg_roic >= 5:
        base_score = 5
    else:
        base_score = 2

    # Consistency bonus/penalty
    consistency_rate = years_above_15 / total_years if total_years > 0 else 0
    if consistency_rate >= 0.8:
        score = min(20, base_score + 2)
    elif consistency_rate >= 0.6:
        score = base_score
    else:
        score = max(0, base_score - 2)

    # Trend adjustment
    if trend == "declining":
        score = max(0, score - 2)
    elif trend == "improving":
        score = min(20, score + 1)

    confidence = "high" if total_years >= 7 else \
                 "medium" if total_years >= 4 else "low"

    return {
        "score": round(score),
        "max": 20,
        "confidence": confidence,
        "avg_roic": round(avg_roic, 1),
        "years_above_15pct": years_above_15,
        "total_years": total_years,
        "trend": trend,
        "values": [round(v, 1) for v in roic_values],
        "note": f"Average ROIC of {avg_roic:.1f}% over {total_years} years, "
                f"{years_above_15}/{total_years} years above 15%"
    }


def score_gross_margin(income_statements):
    """
    Score gross margin stability as a moat proxy.
    Max 20 points.

    Guardrail: declining margins = moat erosion = value trap risk
    """
    if not income_statements:
        return {"score": 0, "max": 20, "confidence": "low",
                "detail": None, "values": [], "note": "No data available"}

    margins = []
    for stmt in income_statements:
        revenue = stmt.get("revenue", 0)
        gross_profit = stmt.get("grossProfit", 0)
        if revenue and revenue > 0:
            margins.append((gross_profit / revenue) * 100)

    if not margins:
        return {"score": 0, "max": 20, "confidence": "low",
                "detail": None, "values": [], "note": "Gross margin data unavailable"}

    avg_margin = sum(margins) / len(margins)
    total_years = len(margins)

    # Trend analysis
    if len(margins) >= 3:
        recent_avg = sum(margins[:3]) / 3
        old_avg = sum(margins[-3:]) / 3
        margin_change = recent_avg - old_avg
        if margin_change > 3:
            trend = "expanding"
        elif margin_change < -3:
            trend = "declining"
        else:
            trend = "stable"
    else:
        trend = "insufficient data"
        margin_change = 0

    # Score based on level and stability
    if avg_margin >= 50:
        base_score = 18
    elif avg_margin >= 35:
        base_score = 15
    elif avg_margin >= 25:
        base_score = 12
    elif avg_margin >= 15:
        base_score = 8
    elif avg_margin >= 5:
        base_score = 4
    else:
        base_score = 1

    if trend == "expanding":
        score = min(20, base_score + 2)
    elif trend == "stable":
        score = base_score
    elif trend == "declining":
        score = max(0, base_score - 4)  # Significant penalty for moat erosion
    else:
        score = base_score

    value_trap_flag = trend == "declining" and len(margins) >= 5

    confidence = "high" if total_years >= 7 else \
                 "medium" if total_years >= 4 else "low"

    return {
        "score": round(score),
        "max": 20,
        "confidence": confidence,
        "avg_margin": round(avg_margin, 1),
        "trend": trend,
        "margin_change_pct": round(margin_change, 1) if len(margins) >= 3 else None,
        "total_years": total_years,
        "values": [round(m, 1) for m in margins],
        "value_trap_flag": value_trap_flag,
        "note": f"Average gross margin {avg_margin:.1f}% ({trend} trend)"
    }


def score_debt_safety(balance_sheets, income_statements):
    """
    Score financial safety / debt levels.
    Max 20 points.

    Key metrics:
    - Debt-to-equity ratio
    - Interest coverage ratio
    - Current ratio (liquidity)
    """
    if not balance_sheets or not income_statements:
        return {"score": 10, "max": 20, "confidence": "low",
                "note": "Insufficient data for debt analysis"}

    latest_balance = balance_sheets[0] if balance_sheets else {}
    latest_income = income_statements[0] if income_statements else {}

    total_debt = latest_balance.get("totalDebt", 0) or 0
    equity = latest_balance.get("totalStockholdersEquity", 0) or 1
    cash = latest_balance.get("cashAndCashEquivalents", 0) or 0
    current_assets = latest_balance.get("totalCurrentAssets", 0) or 0
    current_liabilities = latest_balance.get("totalCurrentLiabilities", 0) or 1
    ebit = latest_income.get("operatingIncome", 0) or 0
    interest_expense = abs(latest_income.get("interestExpense", 0) or 0)

    net_debt = max(0, total_debt - cash)
    debt_to_equity = total_debt / max(equity, 1)
    current_ratio = current_assets / max(current_liabilities, 1)
    interest_coverage = ebit / max(interest_expense, 1) if interest_expense > 0 else 999

    # Debt-to-equity scoring
    if debt_to_equity < 0.3:
        de_score = 8
    elif debt_to_equity < 0.7:
        de_score = 6
    elif debt_to_equity < 1.5:
        de_score = 4
    elif debt_to_equity < 3:
        de_score = 2
    else:
        de_score = 0

    # Interest coverage scoring
    if interest_coverage >= 10 or interest_coverage == 999:
        ic_score = 7
    elif interest_coverage >= 5:
        ic_score = 5
    elif interest_coverage >= 3:
        ic_score = 3
    elif interest_coverage >= 1.5:
        ic_score = 1
    else:
        ic_score = 0

    # Current ratio scoring
    if current_ratio >= 2:
        cr_score = 5
    elif current_ratio >= 1.5:
        cr_score = 4
    elif current_ratio >= 1:
        cr_score = 2
    else:
        cr_score = 0

    score = de_score + ic_score + cr_score

    return {
        "score": round(score),
        "max": 20,
        "confidence": "high",
        "debt_to_equity": round(debt_to_equity, 2),
        "interest_coverage": round(interest_coverage, 1) if interest_coverage != 999 else None,
        "current_ratio": round(current_ratio, 2),
        "net_debt": round(net_debt / 1e9, 2),  # In billions
        "note": f"D/E ratio {debt_to_equity:.2f}, interest coverage "
                f"{'n/a (no debt)' if interest_coverage == 999 else f'{interest_coverage:.1f}x'}"
    }


def score_owner_earnings(owner_earnings_data, cashflow_statements, income_statements):
    """
    Score owner earnings quality.
    Max 20 points.

    Owner Earnings = Net Income + D&A - Maintenance Capex +/- Working Capital
    Maintenance capex estimated as 50-70% of total capex for asset-heavy companies.

    Guardrail: flag when company has changed significantly
    """
    if not cashflow_statements or not income_statements:
        return {"score": 8, "max": 20, "confidence": "low",
                "note": "Insufficient data for owner earnings calculation"}

    owner_earnings_list = []

    # Try using FMP's owner earnings endpoint first
    if owner_earnings_data and isinstance(owner_earnings_data, list):
        for item in owner_earnings_data[:10]:
            oe = item.get("ownerEarnings")
            if oe is not None:
                owner_earnings_list.append(float(oe))

    # Fall back to manual calculation
    if not owner_earnings_list:
        for cf, inc in zip(cashflow_statements[:10], income_statements[:10]):
            net_income = inc.get("netIncome", 0) or 0
            da = cf.get("depreciationAndAmortization", 0) or 0
            total_capex = abs(cf.get("capitalExpenditure", 0) or 0)
            # Estimate maintenance capex as 60% of total (conservative middle estimate)
            maintenance_capex = total_capex * 0.6
            wc_change = cf.get("changeInWorkingCapital", 0) or 0
            oe = net_income + da - maintenance_capex + wc_change
            owner_earnings_list.append(oe)

    if not owner_earnings_list:
        return {"score": 8, "max": 20, "confidence": "low",
                "note": "Could not calculate owner earnings"}

    avg_oe = sum(owner_earnings_list) / len(owner_earnings_list)
    latest_oe = owner_earnings_list[0] if owner_earnings_list else 0

    # Is owner earnings positive and growing?
    positive_years = sum(1 for v in owner_earnings_list if v > 0)
    total_years = len(owner_earnings_list)

    if len(owner_earnings_list) >= 3:
        recent_avg = sum(owner_earnings_list[:3]) / 3
        old_avg = sum(owner_earnings_list[-3:]) / 3
        growth = ((recent_avg / old_avg) - 1) * 100 if old_avg > 0 else 0
        trend = "growing" if growth > 10 else "declining" if growth < -10 else "stable"
    else:
        growth = 0
        trend = "insufficient data"

    # Score
    positive_rate = positive_years / total_years
    if positive_rate >= 0.9 and trend == "growing":
        score = 18
    elif positive_rate >= 0.9 and trend == "stable":
        score = 15
    elif positive_rate >= 0.8:
        score = 12
    elif positive_rate >= 0.6:
        score = 8
    elif positive_rate >= 0.4:
        score = 4
    else:
        score = 1

    confidence = "medium"  # Always medium - maintenance capex is estimated
    note = f"Owner earnings positive in {positive_years}/{total_years} years, {trend} trend. " \
           f"Note: maintenance capex estimated at 60% of total capex."

    return {
        "score": round(score),
        "max": 20,
        "confidence": confidence,
        "avg_owner_earnings": round(avg_oe / 1e9, 2),  # billions
        "latest_owner_earnings": round(latest_oe / 1e9, 2),
        "trend": trend,
        "positive_years": positive_years,
        "total_years": total_years,
        "note": note,
        "caveat": "Maintenance capex is estimated - this figure is approximate"
    }


def score_capital_allocation(income_statements, balance_sheets, price_data):
    """
    Score capital allocation quality.
    Max 20 points.

    Method: Return on Retained Earnings
    If company retained $X over 10 years, how much did market cap grow?
    Great managers create $3-5 of market value per $1 retained.
    Bad managers destroy value despite retaining earnings.

    Also checks: buyback timing quality, SBC as % of revenue
    """
    if not income_statements or not balance_sheets:
        return {"score": 10, "max": 20, "confidence": "low",
                "note": "Insufficient data for capital allocation analysis"}

    # Calculate retained earnings over available period
    total_retained = 0
    total_dividends = 0
    sbc_values = []

    for cf_stmt in income_statements[:10]:
        net_income = cf_stmt.get("netIncome", 0) or 0
        revenue = cf_stmt.get("revenue", 0) or 1
        sbc = cf_stmt.get("stockBasedCompensation", 0) or 0
        sbc_values.append(sbc / revenue * 100 if revenue > 0 else 0)

    # Get shares outstanding trend (buyback signal)
    shares_trend = []
    for bs in balance_sheets[:10]:
        shares = bs.get("commonStock", None)
        if shares is not None:
            shares_trend.append(float(shares))

    # Check share count reduction (cannibal signal)
    if len(shares_trend) >= 3:
        share_change_pct = ((shares_trend[0] / shares_trend[-1]) - 1) * 100
        if share_change_pct < -20:
            buyback_score = 8  # Aggressive cannibal - excellent
        elif share_change_pct < -10:
            buyback_score = 6
        elif share_change_pct < -3:
            buyback_score = 4
        elif share_change_pct < 5:
            buyback_score = 3  # Roughly flat
        else:
            buyback_score = 0  # Diluting shareholders
        shares_reduced = share_change_pct < 0
    else:
        buyback_score = 3  # Neutral if no data
        share_change_pct = 0
        shares_reduced = None

    # SBC as % of revenue (lower is better)
    avg_sbc_pct = sum(sbc_values) / len(sbc_values) if sbc_values else 0
    if avg_sbc_pct < 1:
        sbc_score = 7
    elif avg_sbc_pct < 3:
        sbc_score = 5
    elif avg_sbc_pct < 5:
        sbc_score = 3
    elif avg_sbc_pct < 10:
        sbc_score = 1
    else:
        sbc_score = 0

    # Note: We deliberately don't try to calculate return on retained earnings
    # without reliable historical market cap data - better to be honest about
    # data limitations than give a false precise number

    score = buyback_score + sbc_score
    score = min(20, max(0, score))

    return {
        "score": round(score),
        "max": 20,
        "confidence": "medium",
        "share_count_change_pct": round(share_change_pct, 1) if len(shares_trend) >= 3 else None,
        "shares_reduced": shares_reduced,
        "avg_sbc_pct_of_revenue": round(avg_sbc_pct, 1),
        "note": f"Share count {'reduced' if shares_reduced else 'increased' if shares_reduced is False else 'unknown'} "
                f"by {abs(share_change_pct):.1f}% over available period. "
                f"SBC averages {avg_sbc_pct:.1f}% of revenue."
    }


# -- Value Score Components ----------------------------------------------------

def score_normalized_earnings(income_statements, price_data):
    """
    Score normalized P/E vs current price.
    Max 30 points.

    Uses 7-10 year average EPS as 'normalized' earnings.
    Guardrail: flag if business has changed significantly.
    """
    if not income_statements or not price_data:
        return {"score": 15, "max": 30, "confidence": "low",
                "note": "Insufficient data"}

    current_price = None
    if isinstance(price_data, list) and price_data:
        p = price_data[0]
        current_price = p.get("price") or p.get("lastPrice") or p.get("close")
    elif isinstance(price_data, dict):
        current_price = price_data.get("price") or price_data.get("lastPrice") or price_data.get("close")

    if not current_price:
        return {"score": 15, "max": 30, "confidence": "low",
                "note": "Could not determine current price"}
    
    current_price = float(current_price)

    # Get EPS over available years
    eps_values = []
    for stmt in income_statements:
        eps = stmt.get("eps")
        if eps is not None and float(eps) > 0:
            eps_values.append(float(eps))

    if not eps_values:
        return {"score": 15, "max": 30, "confidence": "low",
                "note": "EPS data unavailable"}

    years = len(eps_values)

    # Detect high-growth companies where 10yr average is misleading
    # If recent EPS is 3x+ the oldest EPS, company has grown significantly
    consistent_growth = False
    if len(eps_values) >= 5 and eps_values[-1] > 0:
        growth_multiple = eps_values[0] / eps_values[-1]
        consistent_growth = growth_multiple >= 2.5

    # For high-growth companies use 5-year average, otherwise 10-year
    if consistent_growth and len(eps_values) >= 5:
        normalized_eps = sum(eps_values[:5]) / 5
        normalization_note = "5-year average used (consistent growth makes 10-yr average misleading)"
    else:
        normalized_eps = sum(eps_values) / len(eps_values)
        normalization_note = f"{years}-year average"

    current_pe = current_price / normalized_eps if normalized_eps > 0 else 999

    # Check for business model change - if EPS variance is very high,
    # normalized earnings may be less meaningful
    if len(eps_values) >= 5:
        avg = sum(eps_values) / len(eps_values)
        variance = sum((v - avg) ** 2 for v in eps_values) / len(eps_values)
        cv = (variance ** 0.5) / avg if avg > 0 else 0
        business_changed_flag = cv > 0.5
    else:
        business_changed_flag = False

    if current_pe < 10:
        score = 28
        valuation = "Very cheap vs normalized earnings"
    elif current_pe < 15:
        score = 24
        valuation = "Cheap vs normalized earnings"
    elif current_pe < 20:
        score = 20
        valuation = "Fair value vs normalized earnings"
    elif current_pe < 25:
        score = 15
        valuation = "Slight premium to normalized earnings"
    elif current_pe < 35:
        score = 10
        valuation = "Expensive vs normalized earnings"
    else:
        score = 5
        valuation = "Very expensive vs normalized earnings"

    # Check for business model change - if EPS variance is very high,
    # normalized earnings may be less meaningful
    if len(eps_values) >= 5:
        avg = sum(eps_values) / len(eps_values)
        variance = sum((v - avg) ** 2 for v in eps_values) / len(eps_values)
        cv = (variance ** 0.5) / avg if avg > 0 else 0
        business_changed_flag = cv > 0.5  # High variance suggests business change
    else:
        business_changed_flag = False

    confidence = "high" if years >= 7 else "medium" if years >= 4 else "low"

    return {
        "score": round(score),
        "max": 30,
        "confidence": confidence,
        "normalized_eps": round(normalized_eps, 2),
        "current_pe_normalized": round(current_pe, 1),
        "current_price": round(current_price, 2),
        "years_of_data": years,
        "valuation": valuation,
        "business_changed_flag": business_changed_flag or consistent_growth,
        "caveat": ("5-year average used instead of 10-year because this company has grown EPS significantly -- "
                   "the long-term average would understate true earnings power.") if consistent_growth else (
                   "Business model may have changed significantly" if business_changed_flag else None),
        "note": f"Normalized P/E of {current_pe:.1f}x based on {normalization_note}, EPS ${normalized_eps:.2f}"
    }


def score_fcf_yield(cashflow_statements, price_data, balance_sheets):
    """
    Score FCF yield attractiveness.
    Max 30 points.

    FCF Yield = Free Cash Flow / Market Cap
    Higher is better (more cash returned per dollar invested).
    """
    if not cashflow_statements or not price_data:
        return {"score": 15, "max": 30, "confidence": "low",
                "note": "Insufficient data"}

    current_price = None
    shares_outstanding = None

    if isinstance(price_data, list) and price_data:
        p = price_data[0]
        current_price = p.get("price")
        # FMP uses different field names depending on endpoint
        shares_outstanding = (p.get("sharesOutstanding") or
                              p.get("shares") or
                              p.get("commonStockSharesOutstanding"))
        # Try market cap directly if available
        market_cap_direct = p.get("marketCap") or p.get("mktCap")
    elif isinstance(price_data, dict):
        current_price = price_data.get("price")
        shares_outstanding = (price_data.get("sharesOutstanding") or
                              price_data.get("shares") or
                              price_data.get("commonStockSharesOutstanding"))
        market_cap_direct = price_data.get("marketCap") or price_data.get("mktCap")
    else:
        market_cap_direct = None

    if not current_price:
        return {"score": 15, "max": 30, "confidence": "low",
                "note": "Could not determine current price"}

    # Use direct market cap if available, otherwise calculate
    if market_cap_direct:
        market_cap = float(market_cap_direct)
    elif shares_outstanding:
        market_cap = float(current_price) * float(shares_outstanding)
    else:
        return {"score": 15, "max": 30, "confidence": "low",
                "note": "Could not determine market cap - sharesOutstanding not available"}

    current_price = float(current_price)

    # Calculate FCF for available years
    fcf_values = []
    for stmt in cashflow_statements:
        op_cf = stmt.get("operatingCashFlow", 0) or 0
        capex = abs(stmt.get("capitalExpenditure", 0) or 0)
        fcf = op_cf - capex
        fcf_values.append(fcf)

    if not fcf_values:
        return {"score": 15, "max": 30, "confidence": "low",
                "note": "FCF data unavailable"}

    # Use average FCF for yield calculation (more stable than single year)
    avg_fcf = sum(fcf_values[:5]) / min(5, len(fcf_values))  # 5-year avg
    latest_fcf = fcf_values[0] if fcf_values else 0

    fcf_yield = (avg_fcf / market_cap * 100) if market_cap > 0 else 0

    if fcf_yield >= 8:
        score = 28
        assessment = "Very attractive FCF yield"
    elif fcf_yield >= 5:
        score = 23
        assessment = "Attractive FCF yield"
    elif fcf_yield >= 3:
        score = 18
        assessment = "Moderate FCF yield"
    elif fcf_yield >= 1.5:
        score = 12
        assessment = "Below average FCF yield"
    elif fcf_yield >= 0:
        score = 6
        assessment = "Low FCF yield"
    else:
        score = 0
        assessment = "Negative FCF (burning cash)"

    # FCF trend
    if len(fcf_values) >= 3:
        recent = sum(fcf_values[:3]) / 3
        older = sum(fcf_values[-3:]) / 3
        fcf_trend = "growing" if recent > older * 1.1 else \
                    "declining" if recent < older * 0.9 else "stable"
    else:
        fcf_trend = "insufficient data"

    return {
        "score": round(score),
        "max": 30,
        "confidence": "medium",
        "fcf_yield_pct": round(fcf_yield, 1),
        "avg_fcf_billions": round(avg_fcf / 1e9, 2),
        "market_cap_billions": round(market_cap / 1e9, 1),
        "fcf_trend": fcf_trend,
        "assessment": assessment,
        "note": f"FCF yield of {fcf_yield:.1f}% based on 5-year average FCF"
    }


def score_dcf_value(dcf_data, price_data):
    """
    Score discount to intrinsic value based on DCF.
    Max 40 points.

    IMPORTANT: DCF models are highly sensitive to assumptions.
    We show a range and always communicate uncertainty.
    """
    if not dcf_data or not price_data:
        return {"score": 20, "max": 40, "confidence": "low",
                "note": "DCF data unavailable"}

    dcf_value = None
    if isinstance(dcf_data, list) and dcf_data:
        dcf_value = dcf_data[0].get("dcf")
    elif isinstance(dcf_data, dict):
        dcf_value = dcf_data.get("dcf")

    current_price = None
    if isinstance(price_data, list) and price_data:
        current_price = price_data[0].get("price")
    elif isinstance(price_data, dict):
        current_price = price_data.get("price")

    if not dcf_value or not current_price:
        return {"score": 20, "max": 40, "confidence": "low",
                "note": "Could not retrieve DCF or current price"}

    dcf_value = float(dcf_value)
    current_price = float(current_price)

    # Calculate discount/premium
    discount_pct = ((dcf_value - current_price) / dcf_value) * 100

    # Create a range to show uncertainty (+/- 20% on DCF estimate)
    dcf_low = dcf_value * 0.8
    dcf_high = dcf_value * 1.2

    if discount_pct >= 40:
        score = 38
        assessment = "Trading at a large discount to estimated intrinsic value"
    elif discount_pct >= 25:
        score = 32
        assessment = "Significant margin of safety vs estimated intrinsic value"
    elif discount_pct >= 10:
        score = 26
        assessment = "Moderate margin of safety"
    elif discount_pct >= 0:
        score = 20
        assessment = "Trading near estimated intrinsic value"
    elif discount_pct >= -15:
        score = 13
        assessment = "Slight premium to estimated intrinsic value"
    elif discount_pct >= -30:
        score = 7
        assessment = "Significant premium to estimated intrinsic value"
    else:
        score = 2
        assessment = "Trading well above estimated intrinsic value"

    return {
        "score": round(score),
        "max": 40,
        "confidence": "low",  # DCF always low confidence due to assumption sensitivity
        "dcf_estimate": round(dcf_value, 2),
        "dcf_range_low": round(dcf_low, 2),
        "dcf_range_high": round(dcf_high, 2),
        "current_price": round(current_price, 2),
        "discount_pct": round(discount_pct, 1),
        "assessment": assessment,
        "caveat": "DCF models are highly sensitive to assumptions about future growth and discount rates. "
                  "This is a rough compass, not a precise measurement. Always treat intrinsic value as a range.",
        "note": f"DCF estimate ${dcf_value:.2f} (range ${dcf_low:.0f}-${dcf_high:.0f}) vs current ${current_price:.2f}"
    }


# -- Red Flags ----------------------------------------------------------------

def detect_red_flags(data):
    """
    Detect specific red flags that should be prominently displayed.
    Returns list of dicts with flag details.
    """
    flags = []
    income = data.get("income", []) or []
    balance = data.get("balance", []) or []
    cashflow = data.get("cashflow", []) or []
    insider = data.get("insider", []) or []

    # 1. Declining gross margins (3+ years)
    if len(income) >= 4:
        margins = []
        for stmt in income[:5]:
            rev = stmt.get("revenue", 0) or 0
            gp = stmt.get("grossProfit", 0) or 0
            if rev > 0:
                margins.append(gp / rev * 100)
        if len(margins) >= 4:
            if all(margins[i] < margins[i+1] for i in range(min(3, len(margins)-1))):
                flags.append({
                    "type": "declining_margins",
                    "severity": "high",
                    "title": "Declining gross margins for 3+ consecutive years",
                    "detail": f"Gross margin fell from {margins[-1]:.1f}% to {margins[0]:.1f}% "
                              f"over {len(margins)} years",
                    "why_it_matters": "Declining margins suggest the company is losing pricing power "
                                      "or facing increasing competition -- a classic value trap signal.",
                    "historical_example": "Kodak showed declining margins for years before bankruptcy. "
                                          "Sears had similar patterns a decade before collapse."
                })

    # 2. Rising debt faster than earnings
    if len(balance) >= 3 and len(income) >= 3:
        old_debt = balance[-1].get("totalDebt", 0) or 0
        new_debt = balance[0].get("totalDebt", 0) or 0
        old_income = income[-1].get("netIncome", 0) or 1
        new_income = income[0].get("netIncome", 0) or 1

        if old_debt > 0 and new_debt > 0:
            debt_growth = (new_debt / old_debt - 1) * 100
            income_growth = (new_income / old_income - 1) * 100
            if debt_growth > income_growth + 30 and debt_growth > 50:
                flags.append({
                    "type": "debt_growing_faster_than_earnings",
                    "severity": "medium",
                    "title": "Debt growing significantly faster than earnings",
                    "detail": f"Debt grew {debt_growth:.0f}% while net income grew {income_growth:.0f}%",
                    "why_it_matters": "Companies that borrow faster than they earn are "
                                      "increasingly fragile and may struggle in downturns.",
                    "historical_example": None
                })

    # 3. Insider cluster selling (open market, 3+ insiders, 60 days)
    if insider and isinstance(insider, list):
        cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        recent_sells = [t for t in insider
                        if t.get("transactionDate", "") >= cutoff
                        and t.get("acquistionOrDisposition", "") == "D"
                        and t.get("transactionType", "") in ["S-Sale", "S-Sale+OE"]]
        if len(recent_sells) >= 3:
            total_value = sum(
                (t.get("securitiesTransacted", 0) or 0) * (t.get("price", 0) or 0)
                for t in recent_sells
            )
            flags.append({
                "type": "insider_cluster_selling",
                "severity": "medium",
                "title": f"Cluster insider selling: {len(recent_sells)} insiders sold in last 90 days",
                "detail": f"Total value sold: ~${total_value/1e6:.1f}M",
                "why_it_matters": "When multiple insiders sell simultaneously, it can signal "
                                  "concerns about near-term prospects. Note: insiders sell for "
                                  "many reasons -- this is one signal, not a verdict.",
                "historical_example": None
            })

    # 4. Negative FCF in multiple recent years
    if len(cashflow) >= 3:
        neg_fcf_years = 0
        for cf in cashflow[:5]:
            op = cf.get("operatingCashFlow", 0) or 0
            capex = abs(cf.get("capitalExpenditure", 0) or 0)
            if op - capex < 0:
                neg_fcf_years += 1
        if neg_fcf_years >= 3:
            flags.append({
                "type": "negative_fcf",
                "severity": "high",
                "title": f"Negative free cash flow in {neg_fcf_years} of last 5 years",
                "detail": "The company is spending more than it generates from operations",
                "why_it_matters": "A company that can't generate cash must borrow or dilute "
                                  "shareholders to survive. This is only acceptable for early-stage "
                                  "companies with clear path to profitability.",
                "historical_example": None
            })

    # 5. High goodwill (acquisition risk)
    if balance:
        latest = balance[0]
        goodwill = latest.get("goodwill", 0) or 0
        total_assets = latest.get("totalAssets", 1) or 1
        goodwill_pct = goodwill / total_assets * 100
        if goodwill_pct > 40:
            flags.append({
                "type": "high_goodwill",
                "severity": "medium",
                "title": f"High goodwill: {goodwill_pct:.0f}% of total assets",
                "detail": f"${goodwill/1e9:.1f}B of goodwill on balance sheet",
                "why_it_matters": "Goodwill represents premium paid for acquisitions. "
                                  "High goodwill means large impairment risk if acquisitions "
                                  "underperform -- this can result in sudden large losses.",
                "historical_example": "AOL Time Warner wrote off $99B of goodwill in 2002 "
                                      "after its disastrous merger."
            })

    return flags


# -- Value Trap Check ----------------------------------------------------------

def check_value_trap(data, gross_margin_result):
    """
    Check for value trap signals.
    Returns dict with trap_risk level and triggered signals.
    """
    signals = []
    income = data.get("income", []) or []
    cashflow = data.get("cashflow", []) or []

    # 1. Declining gross margins (already calculated)
    if gross_margin_result.get("value_trap_flag"):
        signals.append({
            "signal": "Declining gross margins",
            "status": "triggered",
            "detail": gross_margin_result.get("note", "")
        })
    else:
        signals.append({
            "signal": "Declining gross margins",
            "status": "clear",
            "detail": "Margins stable or improving"
        })

    # 2. Revenue declining
    if len(income) >= 3:
        revenues = [stmt.get("revenue", 0) or 0 for stmt in income[:5]]
        if revenues[0] < revenues[-1] * 0.9:
            signals.append({
                "signal": "Declining revenue",
                "status": "triggered",
                "detail": f"Revenue declined over available period"
            })
        else:
            signals.append({
                "signal": "Declining revenue",
                "status": "clear",
                "detail": "Revenue growing or stable"
            })
    else:
        signals.append({
            "signal": "Declining revenue",
            "status": "unknown",
            "detail": "Insufficient data"
        })

    # 3. FCF consistently negative
    if len(cashflow) >= 3:
        neg_count = sum(1 for cf in cashflow[:5]
                        if (cf.get("operatingCashFlow", 0) or 0) -
                           abs(cf.get("capitalExpenditure", 0) or 0) < 0)
        if neg_count >= 3:
            signals.append({
                "signal": "Persistent negative free cash flow",
                "status": "triggered",
                "detail": f"Negative FCF in {neg_count} of last 5 years"
            })
        else:
            signals.append({
                "signal": "Persistent negative free cash flow",
                "status": "clear",
                "detail": "Free cash flow generally positive"
            })
    else:
        signals.append({
            "signal": "Persistent negative free cash flow",
            "status": "unknown",
            "detail": "Insufficient data"
        })

    # 4. Earnings per share declining
    if len(income) >= 4:
        eps_values = [stmt.get("eps", 0) or 0 for stmt in income[:5]]
        if eps_values[0] < eps_values[-1] * 0.85 and eps_values[-1] > 0:
            signals.append({
                "signal": "Declining earnings per share",
                "status": "triggered",
                "detail": f"EPS declined significantly over available period"
            })
        else:
            signals.append({
                "signal": "Declining earnings per share",
                "status": "clear",
                "detail": "EPS stable or growing"
            })
    else:
        signals.append({"signal": "Declining earnings per share",
                        "status": "unknown", "detail": "Insufficient data"})

    triggered = sum(1 for s in signals if s["status"] == "triggered")

    if triggered >= 3:
        risk_level = "high"
        risk_label = "High Value Trap Risk"
        risk_color = "red"
    elif triggered >= 2:
        risk_level = "medium"
        risk_label = "Moderate Value Trap Risk"
        risk_color = "yellow"
    else:
        risk_level = "low"
        risk_label = "Low Value Trap Risk"
        risk_color = "green"

    return {
        "risk_level": risk_level,
        "risk_label": risk_label,
        "risk_color": risk_color,
        "triggered_count": triggered,
        "signals": signals,
        "explanation": "A value trap is a stock that looks cheap but is cheap because "
                       "the business is permanently declining, not temporarily misunderstood."
    }


# -- Insider Signal ------------------------------------------------------------

def analyze_insider_activity(insider_data):
    """
    Analyze insider buying/selling with proper guardrails.
    Only counts open market purchases/sales, not awards.
    Cluster signal requires 3+ insiders within 90 days.
    """
    if not insider_data or not isinstance(insider_data, list):
        return {"signal": "neutral", "detail": "No recent insider data", "confidence": "low"}

    cutoff_90 = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    cutoff_180 = (datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d")

    # Only count open market buys (not awards, grants, etc.)
    open_market_buy_types = ["P-Purchase", "P-Purchase+OE"]
    open_market_sell_types = ["S-Sale", "S-Sale+OE"]

    recent_buys = [t for t in insider_data
                   if t.get("transactionDate", "") >= cutoff_90
                   and t.get("transactionType", "") in open_market_buy_types]

    recent_sells = [t for t in insider_data
                    if t.get("transactionDate", "") >= cutoff_90
                    and t.get("transactionType", "") in open_market_sell_types]

    buy_value = sum((t.get("securitiesTransacted", 0) or 0) * (t.get("price", 0) or 0)
                    for t in recent_buys)
    sell_value = sum((t.get("securitiesTransacted", 0) or 0) * (t.get("price", 0) or 0)
                     for t in recent_sells)

    unique_buyers = len(set(t.get("reportingName", "") for t in recent_buys))
    unique_sellers = len(set(t.get("reportingName", "") for t in recent_sells))

    # Cluster buying signal (3+ insiders, open market, meaningful value)
    if unique_buyers >= 3 and buy_value >= 1_000_000:
        signal = "cluster_buying"
        confidence = "medium"
        detail = (f"{unique_buyers} insiders made open market purchases totaling "
                  f"${buy_value/1e6:.1f}M in last 90 days -- a meaningful cluster buying signal. "
                  f"Note: even cluster buying does not guarantee returns.")
    elif unique_sellers >= 3 and sell_value >= 5_000_000:
        signal = "cluster_selling"
        confidence = "medium"
        detail = (f"{unique_sellers} insiders sold open market totaling "
                  f"${sell_value/1e6:.1f}M in last 90 days.")
    elif unique_buyers >= 1 and buy_value >= 500_000:
        signal = "some_buying"
        confidence = "low"
        detail = (f"Some insider buying detected (${buy_value/1e6:.1f}M) but not a strong cluster signal. "
                  f"Individual purchases have limited predictive value.")
    elif unique_sellers >= 1:
        signal = "some_selling"
        confidence = "low"
        detail = ("Some insider selling detected. Note: insiders sell for many reasons "
                  "(taxes, diversification, personal needs) -- selling alone means little.")
    else:
        signal = "neutral"
        confidence = "low"
        detail = "No significant insider buying or selling in last 90 days."

    return {
        "signal": signal,
        "confidence": confidence,
        "detail": detail,
        "unique_buyers_90d": unique_buyers,
        "unique_sellers_90d": unique_sellers,
        "buy_value": round(buy_value / 1e6, 1),
        "sell_value": round(sell_value / 1e6, 1),
        "important_caveat": "Insider buying is one signal. Insiders are frequently wrong about "
                            "timing. This never overrides fundamental analysis."
    }


# -- Main Calculation Engine ---------------------------------------------------

def calculate_scores(data):
    """
    Run all score calculations on fetched data.
    Returns structured scores dict.
    """
    income = data.get("income", []) or []
    cashflow = data.get("cashflow", []) or []
    balance = data.get("balance", []) or []
    key_metrics = data.get("key_metrics", []) or []
    price = data.get("price", []) or []
    dcf = data.get("dcf", []) or []
    insider = data.get("insider", []) or []
    owner_earnings = data.get("owner_earnings", []) or []

    # Quality components
    roic = score_roic(key_metrics)
    gross_margin = score_gross_margin(income)
    debt_safety = score_debt_safety(balance, income)
    oe = score_owner_earnings(owner_earnings, cashflow, income)
    cap_alloc = score_capital_allocation(income, balance, price)

    quality_score = roic["score"] + gross_margin["score"] + debt_safety["score"] + \
                    oe["score"] + cap_alloc["score"]
    quality_max = 100

    # Value components
    norm_earnings = score_normalized_earnings(income, price)
    fcf_yield = score_fcf_yield(cashflow, price, balance)
    dcf_value = score_dcf_value(dcf, price)

    value_score = norm_earnings["score"] + fcf_yield["score"] + dcf_value["score"]
    value_max = 100

    # Overall score (quality weighted 60%, value 40%)
    overall_score = round((quality_score * 0.6) + (value_score * 0.4))

    # Value trap check
    value_trap = check_value_trap(data, gross_margin)

    # Red flags
    red_flags = detect_red_flags(data)

    # Insider activity
    insider_signal = analyze_insider_activity(insider)

    # Overall label
    if overall_score >= 80:
        overall_label = "Exceptional"
        overall_color = "green"
    elif overall_score >= 65:
        overall_label = "Strong"
        overall_color = "green"
    elif overall_score >= 50:
        overall_label = "Average"
        overall_color = "yellow"
    elif overall_score >= 35:
        overall_label = "Below Average"
        overall_color = "yellow"
    else:
        overall_label = "Poor"
        overall_color = "red"

    # Verdict label (considering value trap and red flags)
    high_flags = sum(1 for f in red_flags if f["severity"] == "high")
    if value_trap["risk_level"] == "high" or high_flags >= 2:
        verdict = "significant_concerns"
    elif overall_score >= 65 and value_trap["risk_level"] == "low":
        verdict = "worth_investigating"
    else:
        verdict = "mixed"

    return {
        "overall_score": overall_score,
        "overall_label": overall_label,
        "overall_color": overall_color,
        "verdict": verdict,
        "quality": {
            "score": quality_score,
            "max": quality_max,
            "components": {
                "roic": roic,
                "gross_margin": gross_margin,
                "debt_safety": debt_safety,
                "owner_earnings": oe,
                "capital_allocation": cap_alloc
            }
        },
        "value": {
            "score": value_score,
            "max": value_max,
            "components": {
                "normalized_earnings": norm_earnings,
                "fcf_yield": fcf_yield,
                "dcf": dcf_value
            }
        },
        "value_trap": value_trap,
        "red_flags": red_flags,
        "insider": insider_signal,
        "analyzed_at": datetime.utcnow().isoformat()
    }


def analyze_stock(symbol):
    """
    Main entry point. Fetches data and calculates all scores.
    Returns complete structured data ready for Claude synthesis.
    """
    print(f"[Analyzer] Starting analysis for {symbol}")
    data = fetch_all_data(symbol)
    print(f"[Analyzer] Data fetched, calculating scores")
    scores = calculate_scores(data)
    print(f"[Analyzer] Scores calculated: overall={scores['overall_score']}")

    # Add company profile
    profile = data.get("profile", [])
    if isinstance(profile, list) and profile:
        profile = profile[0]
    scores["profile"] = {
        "name": profile.get("companyName", symbol),
        "symbol": symbol,
        "sector": profile.get("sector", "Unknown"),
        "industry": profile.get("industry", "Unknown"),
        "description": profile.get("description", ""),
        "website": profile.get("website", ""),
        "exchange": profile.get("exchangeShortName", ""),
        "market_cap": profile.get("mktCap", 0),
        "employees": profile.get("fullTimeEmployees", 0),
        "ceo": profile.get("ceo", ""),
        "country": profile.get("country", ""),
    }

    # Add raw data summary for Claude to reference
    # Only pass aggregated/computed values, not raw statements
    income = data.get("income", []) or []
    cashflow = data.get("cashflow", []) or []

    scores["data_summary"] = {
        "years_of_data": len(income),
        "latest_revenue": income[0].get("revenue") if income else None,
        "latest_net_income": income[0].get("netIncome") if income else None,
        "latest_fcf": (
            (cashflow[0].get("operatingCashFlow", 0) or 0) -
            abs(cashflow[0].get("capitalExpenditure", 0) or 0)
        ) if cashflow else None,
        "latest_eps": income[0].get("eps") if income else None,
        "revenue_5yr_ago": income[4].get("revenue") if len(income) > 4 else None,
    }

    return scores
