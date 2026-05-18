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


# -- Dashboard insights ------------------------------------------------
# Short, action-framed prose for the dashboard CTA cards. See
# INSIGHTS_DESIGN.md and backend/insights.py for the broader feature.

INSIGHTS_SYSTEM_PROMPT = """You write one-sentence headlines for a personal portfolio dashboard.

Rules you must follow:
1. Use ONLY the numbers provided in the prompt. Never invent or estimate a figure.
2. Output ONE sentence, action-framed (suggests reviewing or rethinking something).
3. Be direct. No hedge words like "consider", "might", "should probably".
4. Reference at least one specific number from the inputs.
5. No markdown, no quotes, no preamble. Output the sentence only."""


# --- Risk / news card prompts ---
# Three-pass design: extract -> verify -> synthesize. See INSIGHTS_DESIGN.md §6.

RISK_EXTRACT_SYSTEM = """You categorize raw stock news + analyzer signals into structured "notable items" for a dashboard insights card.

Rules you must follow:
1. ONLY use the inputs provided. Do NOT invent facts, prices, or interpretations not present in a source.
2. Each item you output MUST cite at least one source_id from the SOURCES list using its exact label.
3. Output strict JSON only (no markdown, no preamble) matching the schema described in the user message.
4. Be conservative: if a source is ambiguous, skip it rather than over-interpret.
5. Direction is "upside" or "downside" only. Severity is "info", "warn", or "critical"."""


RISK_VERIFY_SYSTEM = """You audit a list of proposed risk items against their source materials. Your job is to catch ungrounded claims — items whose summary states things the cited sources do not support.

Rules:
1. For each item, confirm every cited source_id appears in SOURCES.
2. Confirm the item's summary is a fair interpretation of what those sources say. If the summary adds facts not in the sources, mark it unsupported.
3. Output strict JSON only. No markdown."""


RISK_SYNTHESIZE_SYSTEM = """You write a 2-3 sentence dashboard summary based on a pre-validated list of risk items. Do NOT introduce facts beyond what the items contain. No markdown, no preamble — just the prose."""


def _render_sources_for_prompt(sources):
    """Compact one-line-per-source rendering Claude can scan quickly."""
    lines = []
    for sid, src in sources.items():
        if src.get("type") == "news":
            published = (src.get("published") or "")[:10]
            site = src.get("site") or ""
            lines.append(
                f"[{sid}] news | {src.get('symbol','?')} | {published} | {site} | "
                f"{(src.get('title') or '')[:140]}"
            )
        else:
            lines.append(
                f"[{sid}] analyzer | {src.get('symbol','?')} | {src.get('kind','?')} "
                f"({src.get('severity','?')}) | {(src.get('title') or '')[:140]}"
            )
    return "\n".join(lines)


def _extract_json(text):
    """Pull the first JSON object/array out of Claude's response, tolerating
    markdown fences and leading/trailing prose."""
    text = (text or "").strip()
    # Strip code fences if present
    if text.startswith("```"):
        text = text.split("```", 2)
        text = text[1] if len(text) > 1 else ""
        if text.lower().startswith("json"):
            text = text[4:].lstrip()
        text = text.split("```", 1)[0]
    # Find the first { or [ and the matching last } or ]
    starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
    if not starts:
        raise ValueError("No JSON found in response")
    start = min(starts)
    end = max(text.rfind("]"), text.rfind("}"))
    if end <= start:
        raise ValueError("Unbalanced JSON brackets in response")
    return json.loads(text[start:end + 1])


def extract_risk_items(inputs, prior_critique=None):
    """Pass 1: extract structured items from raw news + analyzer signals.
    `inputs` is the dict returned by insights.collect_risk_inputs().
    `prior_critique` is the unsupported_claims list from a failed verify pass
    (None on the first attempt). Returns a list of item dicts."""
    sources_text = _render_sources_for_prompt(inputs.get("sources") or {})
    critique_text = ""
    if prior_critique:
        critique_text = (
            "\n\nA previous attempt failed verification. Issues to avoid this time:\n"
            + "\n".join(f"- {c}" for c in prior_critique[:10])
        )

    prompt = f"""SOURCES (use only these; cite by exact id in brackets):
{sources_text}

Output JSON array of items. Each item:
{{
  "symbol":     "<TICKER>",
  "direction":  "upside" | "downside",
  "severity":   "info" | "warn" | "critical",
  "summary":    "<one short factual sentence drawn from the sources>",
  "source_ids": ["<source_id_1>", ...]
}}

Include only items that are clearly notable (regulatory action, upgrade/downgrade, earnings surprise, material insider activity, declared red flag, etc.). Skip routine coverage.{critique_text}

Output the JSON array only — no markdown, no preamble."""
    raw = claude_complete(prompt, RISK_EXTRACT_SYSTEM, max_tokens=2000)
    items = _extract_json(raw)
    if not isinstance(items, list):
        raise ValueError("Extract did not return a JSON array")
    return items


def verify_risk_items(items, sources):
    """Pass 2: verify every item cites real sources and is well-grounded.
    Returns {grounded: bool, unsupported_claims: [strings]}."""
    sources_text = _render_sources_for_prompt(sources)
    items_text   = json.dumps(items, indent=2)

    prompt = f"""SOURCES:
{sources_text}

PROPOSED ITEMS:
{items_text}

For each item:
1. Every source_id in source_ids must appear (exactly) in SOURCES.
2. The summary must be supported by what those cited sources actually say.

Return JSON:
{{
  "grounded": true|false,
  "unsupported_claims": [
    "<one human-readable sentence per ungrounded claim, naming the source_id or item summary>"
  ]
}}

Output JSON only."""
    raw    = claude_complete(prompt, RISK_VERIFY_SYSTEM, max_tokens=600)
    parsed = _extract_json(raw)
    return {
        "grounded":            bool(parsed.get("grounded")),
        "unsupported_claims":  parsed.get("unsupported_claims") or [],
    }


def synthesize_risk_prose(items):
    """Pass 3: turn validated items into a 2-3 sentence dashboard summary.
    Cannot introduce new facts — only sees the items, never the raw sources."""
    items_text = json.dumps(items, indent=2)
    prompt = f"""VALIDATED ITEMS (use only these):
{items_text}

Write a 2-3 sentence summary for the user's portfolio dashboard. Mention specific tickers and group by direction (downside risks vs upside catalysts). Do NOT recommend buy/sell actions. Plain prose, no markdown."""
    return claude_complete(prompt, RISK_SYNTHESIZE_SYSTEM, max_tokens=250).strip()


def summarize_benchmark(stats, max_words=28):
    """Generate the action-framed sentence shown in the benchmark card.
    `stats` is the dict returned by insights.compute_benchmark_comparison().
    Returns the prose string; raises on Claude API failure (caller catches)."""
    default = stats["default_benchmark"]
    default_cmp = next(
        (c for c in stats["comparisons"] if c["benchmark"] == default),
        stats["comparisons"][0],
    )
    period   = stats["period"]
    rows = "\n".join(
        f"- {c['benchmark']}: portfolio {c['portfolio_pct']:+.2f}% vs "
        f"benchmark {c['benchmark_pct']:+.2f}% (delta {c['delta_pp']:+.2f}pp)"
        for c in stats["comparisons"]
    )

    prompt = f"""Portfolio vs benchmark returns over {period['label']} ({period['from']} → {period['to']}).
Use ONLY these numbers:

{rows}

Default benchmark on the dashboard: {default}.

Write one action-framed sentence (max {max_words} words) about how the portfolio is doing versus {default}. Reference at least one specific number. If portfolio is leading by a lot, suggest re-examining what's driving it; if trailing, suggest reviewing allocation. Do not give a buy/sell recommendation."""

    return claude_complete(prompt, INSIGHTS_SYSTEM_PROMPT, max_tokens=140).strip()


def summarize_concentration(stats, max_words=25):
    """Generate the action-framed sentence shown in the concentration card.
    `stats` is the dict returned by insights.compute_concentration().
    Returns the prose string; raises on Claude API failure (caller catches)."""
    c  = stats["concentration"]
    sw = stats["stock_weights"][:5]
    holdings_lines = "\n".join(f"- {s['symbol']}: {s['weight_pct']}%" for s in sw)
    plat_lines     = "\n".join(
        f"- {p['platform']}: {p['weight_pct']}%" for p in stats["platforms"][:4]
    )

    prompt = f"""Portfolio concentration figures (use ONLY these numbers):

- Top 1 stock weight: {c['top_1_pct']}%
- Top 3 stocks combined: {c['top_3_pct']}%
- Top 5 stocks combined: {c['top_5_pct']}%
- Herfindahl index: {c['hhi']} (0 = diverse, 1 = single position)

Top holdings:
{holdings_lines}

By platform:
{plat_lines}

Write one action-framed sentence (max {max_words} words) about what this concentration means and what to think about reviewing. Reference at least one specific number."""

    return claude_complete(prompt, INSIGHTS_SYSTEM_PROMPT, max_tokens=120).strip()
