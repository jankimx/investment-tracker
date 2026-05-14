"""
Claude Synthesis Engine
Takes calculated scores and generates plain English analysis.
All numbers come from the scores dict - Claude never makes up financial data.
"""

import os
import json
import urllib.request

CLAUDE_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-20250514"


def claude_complete(prompt, system, max_tokens=1500):
    """Call Claude API and return text response."""
    if not CLAUDE_API_KEY:
        raise ValueError("ANTHROPIC_API_KEY not configured")

    payload = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=payload,
        headers={
            "x-api-key": CLAUDE_API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode())
            return data["content"][0]["text"]
    except Exception as e:
        print(f"[Claude] API error: {e}")
        raise


SYSTEM_PROMPT = """You are a financial educator helping beginner investors understand stock analysis. 
Your job is to explain complex financial concepts in plain, honest English.

Rules you must follow:
1. ONLY reference numbers that are explicitly provided to you. Never estimate or invent financial figures.
2. If data is limited or uncertain, say so explicitly. Uncertainty is honest and builds trust.
3. Write like a smart, honest friend who knows finance - not a salesperson or a robot.
4. Always present a genuine bear case, not a softened one. Argue it strongly.
5. Never recommend buying or selling. Frame everything as "here's what the data shows."
6. Use analogies to make concepts concrete for beginners.
7. Keep sentences short. Avoid jargon unless you immediately explain it.
8. This is NOT financial advice. Make that clear naturally in your writing, not just as a disclaimer."""


def generate_business_section(profile, scores):
    """Generate plain English business description."""
    prompt = f"""Write a 3-4 sentence description of this company for a beginner investor.

Company: {profile['name']} ({profile['symbol']})
Sector: {profile['sector']}
Industry: {profile['industry']}
Description from filing: {profile['description'][:500] if profile['description'] else 'Not available'}
Employees: {profile.get('employees', 'Unknown'):,} if isinstance({profile.get('employees', 0)}, int) else profile.get('employees', 'Unknown')
CEO: {profile.get('ceo', 'Unknown')}

Answer these 4 things in your response (in plain English, no jargon):
1. What does this company actually do and who pays them?
2. What keeps customers coming back instead of switching to a competitor? (the moat)
3. What is the single biggest risk or threat to this business?
4. What would a skeptic say about investing in this company right now?

Be honest. If the moat is weak, say so. If the biggest risk is serious, say so.
Do not use bullet points. Write in flowing paragraphs."""

    return claude_complete(prompt, SYSTEM_PROMPT, max_tokens=400)


def generate_quality_narrative(scores):
    """Generate narrative explaining the quality score."""
    q = scores["quality"]
    components = q["components"]
    roic = components["roic"]
    gm = components["gross_margin"]
    debt = components["debt_safety"]
    oe = components["owner_earnings"]
    cap = components["capital_allocation"]

    prompt = f"""Explain what this quality analysis means for a beginner investor in 2-3 sentences.

Quality Score: {q['score']}/100

Key data points (use these exact numbers, do not make up others):
- ROIC: {roic.get('avg_roic', 'N/A')}% average over {roic.get('total_years', 'N/A')} years, trend: {roic.get('trend', 'N/A')}
- Gross margin: {gm.get('avg_margin', 'N/A')}% average, trend: {gm.get('trend', 'N/A')}
- Debt-to-equity: {debt.get('debt_to_equity', 'N/A')}
- Interest coverage: {debt.get('interest_coverage', 'none (no debt)')}x
- Owner earnings trend: {oe.get('trend', 'N/A')}
- Share count change: {cap.get('share_count_change_pct', 'N/A')}% over available period

The strongest signal: highlight the most important positive finding.
The main concern: highlight the most important quality concern, if any.

Write for someone who has never invested before. Use an analogy if it helps."""

    return claude_complete(prompt, SYSTEM_PROMPT, max_tokens=200)


def generate_value_narrative(scores):
    """Generate narrative explaining the value score."""
    v = scores["value"]
    ne = v["components"]["normalized_earnings"]
    fcf = v["components"]["fcf_yield"]
    dcf = v["components"]["dcf"]

    prompt = f"""Explain what this valuation analysis means for a beginner investor in 2-3 sentences.

Value Score: {v['score']}/100

Key data points (use these exact numbers):
- Normalized P/E: {ne.get('current_pe_normalized', 'N/A')}x (based on {ne.get('years_of_data', 'N/A')}-year average EPS of ${ne.get('normalized_eps', 'N/A')})
- Current price: ${ne.get('current_price', 'N/A')}
- FCF yield: {fcf.get('fcf_yield_pct', 'N/A')}%
- DCF estimate range: ${dcf.get('dcf_range_low', 'N/A')} - ${dcf.get('dcf_range_high', 'N/A')}
- Discount/premium to DCF midpoint: {dcf.get('discount_pct', 'N/A')}%

Important: The DCF is a rough estimate, not a precise target. Always communicate this uncertainty.
Business changed flag: {ne.get('business_changed_flag', False)}

Explain what "value" means in plain English. Use an analogy.
If the business changed flag is True, mention that normalized earnings may be less reliable."""

    return claude_complete(prompt, SYSTEM_PROMPT, max_tokens=200)


def generate_verdict(scores, portfolio_context=None):
    """
    Generate the three-paragraph verdict.
    Bull case, bear case (argued strongly), what needs to be true.
    """
    profile = scores["profile"]
    overall = scores["overall_score"]
    quality = scores["quality"]["score"]
    value = scores["value"]["score"]
    value_trap = scores["value_trap"]
    red_flags = scores["red_flags"]
    insider = scores["insider"]
    data_summary = scores["data_summary"]

    flag_summary = "\n".join([f"- {f['title']}" for f in red_flags]) if red_flags else "None detected"
    trap_signals = "\n".join([
        f"- {s['signal']}: {s['status']}"
        for s in value_trap["signals"]
    ])

    portfolio_section = ""
    if portfolio_context:
        portfolio_section = f"""
Portfolio context (user's current holdings):
{json.dumps(portfolio_context, indent=2)}
"""

    prompt = f"""Write the verdict section for this stock analysis. Three clearly labeled paragraphs.

Company: {profile['name']} ({profile['symbol']})
Overall Score: {overall}/100
Quality Score: {quality}/100
Value Score: {value}/100
Value Trap Risk: {value_trap['risk_level']}

Key financials (use these, do not make up others):
- Latest revenue: ${(data_summary.get('latest_revenue') or 0)/1e9:.1f}B
- Latest net income: ${(data_summary.get('latest_net_income') or 0)/1e9:.1f}B
- Latest FCF: ${(data_summary.get('latest_fcf') or 0)/1e9:.1f}B
- Years of data: {data_summary.get('years_of_data', 'N/A')}

Red flags detected:
{flag_summary}

Value trap signals:
{trap_signals}

Insider signal: {insider.get('signal', 'neutral')} - {insider.get('detail', '')}

{portfolio_section}

Write exactly three paragraphs with these headers:

**The Bull Case**
[Argue genuinely for why this could be a great investment. Focus on the strongest data points. 3-4 sentences.]

**The Bear Case**  
[Argue STRONGLY against this investment. What would a thoughtful short seller say? What are the scenarios where this fails even if fundamentals look good? Do not soften this. 3-4 sentences.]

**What Would Need to Be True**
[What specific conditions would need to hold for this investment to work out? Be concrete. 2-3 sentences.]

End with one sentence that naturally reminds the reader this is educational analysis, not financial advice, and that value investing requires patience measured in years not months.

Write for a smart beginner. No jargon without explanation."""

    return claude_complete(prompt, SYSTEM_PROMPT, max_tokens=600)


def generate_learning_section(scores):
    """Generate 3 educational takeaways from this specific analysis."""
    profile = scores["profile"]
    components = scores["quality"]["components"]
    most_interesting_signal = max(
        [
            ("ROIC", components["roic"]["score"], components["roic"]["max"]),
            ("Gross Margin", components["gross_margin"]["score"], components["gross_margin"]["max"]),
            ("Debt Safety", components["debt_safety"]["score"], components["debt_safety"]["max"]),
        ],
        key=lambda x: abs(x[1] - x[2]/2)  # Most extreme vs midpoint
    )[0]

    prompt = f"""Write 3 short educational explanations for a beginner investor based on this analysis of {profile['name']}.

Pick the 3 most relevant concepts from this analysis:
- ROIC (the most relevant score here: {components['roic']['score']}/20)
- Gross margin as moat indicator (score: {components['gross_margin']['score']}/20)
- Value trap risk (level: {scores['value_trap']['risk_level']})
- Normalized earnings (relevant if business changed: {scores['value']['components']['normalized_earnings'].get('business_changed_flag', False)})
- Owner earnings vs reported earnings
- Margin of safety

For each concept write:
1. What is it? (1 sentence, plain English, use an analogy)
2. Why does it matter for THIS company specifically? (1 sentence referencing actual data)
3. Where can I learn more? (say "See our learning library")

Format as:
CONCEPT: [name]
WHAT: [explanation with analogy]
WHY HERE: [specific to this company]
LEARN: See our learning library

Keep each explanation to 2-3 sentences total. Write for someone who just started investing."""

    return claude_complete(prompt, SYSTEM_PROMPT, max_tokens=400)


def generate_portfolio_fit(scores, portfolio_holdings):
    """
    Generate portfolio fit analysis if user has portfolio data.
    portfolio_holdings: list of {platform, stock, value, shares}
    """
    if not portfolio_holdings:
        return None

    profile = scores["profile"]
    total_portfolio = sum(h.get("value", 0) for h in portfolio_holdings)

    # Calculate sector concentrations (simplified)
    holdings_summary = []
    for h in portfolio_holdings[:10]:  # Top 10
        holdings_summary.append(f"{h['stock']} (${h.get('value', 0)/1e3:.0f}k)")

    prompt = f"""Analyze whether {profile['name']} ({profile['symbol']}) would be a good fit for this specific portfolio.

Current portfolio holdings: {', '.join(holdings_summary)}
Total portfolio value: ${total_portfolio/1e3:.0f}k
Company being analyzed: {profile['name']} in {profile['sector']} sector

Quality score: {scores['overall_score']}/100
Value trap risk: {scores['value_trap']['risk_level']}

Write 2-3 sentences covering:
1. How would adding this stock affect concentration/diversification?
2. Does this improve or worsen the portfolio's overall quality?
3. What's the key consideration specific to THIS portfolio?

Be specific to their actual holdings. Frame as information, not a recommendation.
End with: "Only you know your full financial situation, risk tolerance, and goals."
Keep it brief and honest."""

    return claude_complete(prompt, SYSTEM_PROMPT, max_tokens=250)


def synthesize_full_report(scores, portfolio_context=None):
    """
    Generate all Claude-written sections of the report.
    Returns dict of section name -> text.
    """
    profile = scores["profile"]
    print(f"[Claude] Generating report for {profile['name']}")

    sections = {}

    # Generate all sections (could parallelize but keeping sequential for simplicity)
    try:
        print("[Claude] Generating business section...")
        sections["business"] = generate_business_section(profile, scores)
    except Exception as e:
        print(f"[Claude] Business section failed: {e}")
        sections["business"] = f"{profile['name']} operates in the {profile['sector']} sector. " \
                                f"Full description temporarily unavailable."

    try:
        print("[Claude] Generating quality narrative...")
        sections["quality_narrative"] = generate_quality_narrative(scores)
    except Exception as e:
        print(f"[Claude] Quality narrative failed: {e}")
        sections["quality_narrative"] = "Quality analysis complete. See individual signal scores above."

    try:
        print("[Claude] Generating value narrative...")
        sections["value_narrative"] = generate_value_narrative(scores)
    except Exception as e:
        print(f"[Claude] Value narrative failed: {e}")
        sections["value_narrative"] = "Valuation analysis complete. See individual signal scores above."

    try:
        print("[Claude] Generating verdict...")
        sections["verdict"] = generate_verdict(scores, portfolio_context)
    except Exception as e:
        print(f"[Claude] Verdict failed: {e}")
        sections["verdict"] = "Verdict generation temporarily unavailable. Please review scores above."

    try:
        print("[Claude] Generating learning section...")
        sections["learning"] = generate_learning_section(scores)
    except Exception as e:
        print(f"[Claude] Learning section failed: {e}")
        sections["learning"] = "Educational content temporarily unavailable."

    if portfolio_context:
        try:
            print("[Claude] Generating portfolio fit...")
            sections["portfolio_fit"] = generate_portfolio_fit(scores, portfolio_context)
        except Exception as e:
            print(f"[Claude] Portfolio fit failed: {e}")
            sections["portfolio_fit"] = None

    print(f"[Claude] Report complete for {profile['name']}")
    return sections
