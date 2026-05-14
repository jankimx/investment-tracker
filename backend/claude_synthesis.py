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
