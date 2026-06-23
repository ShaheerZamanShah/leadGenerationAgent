"""
agents/scorer_agent.py
----------------------
Agent 2 — Lead Qualification & Scoring

Scores each raw lead 0-100 based on:
  - Decision-making authority (30%)
  - Industry AI-readiness (25%)
  - Company size fit (20%)
  - Pain point likelihood (25%)

Filters out leads below LEAD_SCORE_THRESHOLD.
Uses the fast model for cost efficiency (scoring is classification, not generation).
"""

from __future__ import annotations
from langchain_core.messages import SystemMessage, HumanMessage
from state.schema import OutreachState, Lead, EnrichedLead
from utils.helpers import log_agent, safe_json_parse, now_iso
from utils.llm import fast_llm
from config.settings import settings
from prompts.templates import SCORER_SYSTEM, SCORER_USER


# Industry AI-readiness tiers (pre-computed for scoring boost)
HIGH_READINESS_INDUSTRIES = {
    "saas", "e-commerce", "ecommerce", "fintech", "healthcare", "logistics",
    "real estate", "proptech", "hr tech", "hrtech", "legaltech", "legal tech",
    "marketing", "media", "edtech", "insurtech",
}

# Role authority tiers
AUTHORITY_TIERS = {
    "tier1": {"founder", "co-founder", "ceo", "chief executive", "owner"},
    "tier2": {"cto", "coo", "chief technology", "chief operating", "vp", "vice president"},
    "tier3": {"director", "head of", "manager", "lead"},
}

# Ideal company size range
IDEAL_SIZE_KEYWORDS = {"10-50", "11-50", "50-200", "51-200", "1-10", "10-100"}


def scorer_agent(state: OutreachState) -> dict:
    """
    LangGraph node: scores all raw_leads and filters below threshold.
    Runs LLM scoring for nuanced qualification.
    """
    log_agent("ScorerAgent", "📊 Scoring and qualifying leads...", "info")

    raw_leads = state.get("raw_leads", [])
    if not raw_leads:
        log_agent("ScorerAgent", "No leads to score", "warn")
        return {"filtered_leads": [], "scored_leads": []}

    llm = fast_llm(temperature=0.1)
    scored: list[EnrichedLead] = []
    threshold = settings.lead_score_threshold

    for lead in raw_leads:
        try:
            enriched = _score_lead(lead, llm)
            scored.append(enriched)
        except Exception as e:
            log_agent("ScorerAgent", f"Scoring failed for {lead.get('name')}: {e}", "warn")

    # Filter by threshold
    filtered = [l for l in scored if l.get("score", 0) >= threshold]

    # Sort by score descending
    filtered.sort(key=lambda x: x.get("score", 0), reverse=True)

    log_agent(
        "ScorerAgent",
        f"✓ Scored {len(scored)} leads → {len(filtered)} qualify (score ≥ {threshold})",
        "done",
    )

    return {
        "scored_leads": scored,
        "filtered_leads": filtered,
        "current_agent": "scorer",
        "logs": [log_agent("ScorerAgent", f"{len(filtered)}/{len(scored)} leads qualify", "done")],
    }


def _score_lead(lead: Lead, llm) -> EnrichedLead:
    """
    Score a single lead using a combination of:
    1. Rule-based pre-scoring (fast, deterministic)
    2. LLM semantic scoring (nuanced, handles edge cases)
    Final score = weighted average of both.
    """
    # ── Rule-based pre-score ─────────────────────────────────────────────────
    rule_score = _rule_based_score(lead)

    # ── LLM scoring ─────────────────────────────────────────────────────────
    prompt = SCORER_USER.format(
        name=lead.get("name", "Unknown"),
        title=lead.get("title", "Unknown"),
        company=lead.get("company", "Unknown"),
        industry=lead.get("industry", "Unknown"),
        company_size=lead.get("company_size", "Unknown"),
        location=lead.get("location", "Unknown"),
    )

    messages = [
        SystemMessage(content=SCORER_SYSTEM),
        HumanMessage(content=prompt),
    ]

    response = llm.invoke(messages)
    llm_data = safe_json_parse(response.content, fallback={})

    llm_score = int(llm_data.get("score", rule_score))

    # Weighted blend: 60% LLM + 40% rules
    final_score = int(0.6 * llm_score + 0.4 * rule_score)
    final_score = max(0, min(100, final_score))

    # Build enriched lead
    enriched: EnrichedLead = {
        **lead,
        "score": final_score,
        "score_reasons": llm_data.get("reasons", []),
        "recommended_service": llm_data.get("recommended_service", "AI Automation"),
        "best_channel": llm_data.get("best_channel", _infer_channel(lead)),
    }

    return enriched


def _rule_based_score(lead: Lead) -> int:
    """Fast heuristic scoring — doesn't call LLM."""
    score = 0

    # Authority (30 pts)
    title_lower = lead.get("title", "").lower()
    if any(kw in title_lower for kw in AUTHORITY_TIERS["tier1"]):
        score += 30
    elif any(kw in title_lower for kw in AUTHORITY_TIERS["tier2"]):
        score += 22
    elif any(kw in title_lower for kw in AUTHORITY_TIERS["tier3"]):
        score += 14

    # Industry (25 pts)
    industry_lower = lead.get("industry", "").lower()
    if any(ind in industry_lower for ind in HIGH_READINESS_INDUSTRIES):
        score += 25
    else:
        score += 10  # Generic industries still get some points

    # Company size (20 pts)
    size = lead.get("company_size", "").lower()
    if any(s in size for s in IDEAL_SIZE_KEYWORDS):
        score += 20
    elif size not in ("", "unknown"):
        score += 10

    # Has email (15 pts — means we can reach them)
    if lead.get("email") and "@" in lead.get("email", ""):
        score += 15

    # Has LinkedIn (10 pts)
    if lead.get("linkedin_url"):
        score += 10

    return min(score, 100)


def _infer_channel(lead: Lead) -> str:
    """Infer best outreach channel from available data."""
    if lead.get("email") and "@" in lead.get("email", ""):
        return "email"
    if lead.get("linkedin_url"):
        return "linkedin"
    return "email"
