"""
Claude Synthesis Engine
Takes calculated scores and generates plain English analysis.
All numbers come from the scores dict - Claude never makes up financial data.
"""

import os
import json
import urllib.request

CLAUDE_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-sonnet-4-6"


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
            raw = r.read().decode()
            data = json.loads(raw)
            print(f"[Claude] Response keys: {list(data.keys())}")
            if "content" in data:
                return data["content"][0]["text"]
            elif "error" in data:
                print(f"[Claude] API error response: {data['error']}")
                raise ValueError(f"Claude API error: {data['error']}")
            else:
                print(f"[Claude] Unexpected response: {raw[:200]}")
                raise ValueError("Unexpected Claude API response")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"[Claude] HTTP {e.code} error: {body[:300]}")
        raise
    except Exception as e:
        print(f"[Claude] API error: {type(e).__name__}: {e}")
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
    Generate all Claude-written sections in a single API call.
    Returns dict of section name -> text.
    """
    profile = scores["profile"]
    q = scores["quality"]
    v = scores["value"]
    trap = scores["value_trap"]
    flags = scores.get("red_flags", [])
    insider = scores.get("insider", {})
    data = scores.get("data_summary", {})

    print(f"[Claude] Generating report for {profile['name']}")

    flag_text = ", ".join([f["title"] for f in flags]) if flags else "None"
    portfolio_text = ""
    if portfolio_context:
        holdings = ", ".join([h["stock"] for h in portfolio_context[:8]])
        portfolio_text = f"User current holdings: {holdings}"

    prompt = f"""You are analyzing {profile['name']} ({profile['symbol']}) for a beginner investor.

SCORES (do not change these numbers):
- Overall: {scores['overall_score']}/100
- Quality: {q['score']}/100 (ROIC avg: {q['components']['roic'].get('avg_roic','N/A')}%, Gross margin avg: {q['components']['gross_margin'].get('avg_margin','N/A')}%, D/E: {q['components']['debt_safety'].get('debt_to_equity','N/A')})
- Value: {v['score']}/100 (Normalized P/E: {v['components']['normalized_earnings'].get('current_pe_normalized','N/A')}x, FCF yield: {v['components']['fcf_yield'].get('fcf_yield_pct','N/A')}%, DCF discount: {v['components']['dcf'].get('discount_pct','N/A')}%)
- Value trap risk: {trap['risk_level']}
- Red flags: {flag_text}
- Insider signal: {insider.get('signal','neutral')}
- Sector: {profile.get('sector','Unknown')}
{portfolio_text}

Respond with exactly this format. Do not add any headers, markdown, or extra formatting. Use EXACTLY these separators with no modifications:

SECTION_BUSINESS
[3-4 sentences: what the company does, who pays them, what protects them from competition, biggest risk. Plain English, no jargon.]
END_BUSINESS

SECTION_QUALITY
[2-3 sentences explaining the quality score for a beginner. Reference the actual numbers above.]
END_QUALITY

SECTION_VALUE
[2-3 sentences explaining the valuation for a beginner. Reference the actual numbers above.]
END_VALUE

SECTION_VERDICT
**The Bull Case**
[3-4 sentences arguing genuinely for this investment]

**The Bear Case**
[3-4 sentences arguing strongly AGAINST. Do not soften.]

**What Would Need to Be True**
[2-3 sentences on specific conditions for success]

This is educational analysis only, not financial advice.
END_VERDICT

SECTION_LEARNING
CONCEPT: [name]
WHAT: [one sentence plain English with analogy]
WHY HERE: [one sentence specific to this company with actual numbers]

CONCEPT: [name]
WHAT: [one sentence plain English with analogy]
WHY HERE: [one sentence specific to this company with actual numbers]

CONCEPT: [name]
WHAT: [one sentence plain English with analogy]
WHY HERE: [one sentence specific to this company with actual numbers]
END_LEARNING"""

    try:
        response = claude_complete(prompt, SYSTEM_PROMPT, max_tokens=2000)
        print(f"[Claude] Got response, length: {len(response)}")

        # Parse sections using unambiguous SECTION_X / END_X markers
        import re
        sections = {}
        for name in ["BUSINESS", "QUALITY", "VALUE", "VERDICT", "LEARNING"]:
            pattern = re.compile(
                r'SECTION_' + name + r'\s*\n(.*?)\nEND_' + name,
                re.DOTALL
            )
            m = pattern.search(response)
            if m:
                sections[name.lower()] = m.group(1).strip()

        print(f"[Claude] Parsed sections: {list(sections.keys())}")

        # If still nothing parsed, use whole response as verdict
        if not sections:
            print(f"[Claude] Could not parse sections, raw response length: {len(response)}")
            sections["verdict"] = response

        # Map to expected keys
        result = {
            "business": sections.get("business", f"{profile['name']} operates in the {profile['sector']} sector."),
            "quality_narrative": sections.get("quality", "Quality analysis complete. See scores above."),
            "value_narrative": sections.get("value", "Valuation analysis complete. See scores above."),
            "verdict": sections.get("verdict", response if response else "Please review the scores above."),
            "learning": sections.get("learning", ""),
        }

        if portfolio_context:
            result["portfolio_fit"] = None  # Keep simple for now

        print(f"[Claude] Sections generated: {list(result.keys())}")
        return result

    except Exception as e:
        print(f"[Claude] Synthesis failed: {type(e).__name__}: {e}")
        return {
            "business": f"{profile['name']} operates in the {profile['sector']} sector. Analysis generation failed: {str(e)}",
            "quality_narrative": "Quality analysis complete. See individual signal scores above.",
            "value_narrative": "Valuation analysis complete. See individual signal scores above.",
            "verdict": f"Analysis generation failed. Error: {str(e)}",
            "learning": "",
            "portfolio_fit": None
        }
