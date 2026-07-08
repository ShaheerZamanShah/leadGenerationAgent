"""
agents/scorer_agent.py
----------------------
Agent 3 — Lead Qualification & Scoring

Scores each verified lead 0-100 for fit against the campaign brief:
  - Decision-making authority
  - Fit with target industry & goal
  - Company size fit
  - Likelihood they need the offering

A verification bonus rewards leads we could actually confirm are real.
Filters out leads below LEAD_SCORE_THRESHOLD.
"""

from __future__ import annotations
from langchain_core.messages import SystemMessage, HumanMessage
from state.schema import OutreachState, Lead, EnrichedLead, SearchBrief
from utils.helpers import log_agent, safe_json_parse
from utils.llm import invoke_smart_or_fast, RateLimitExhausted
from config.settings import settings
from prompts.templates import SCORER_SYSTEM, SCORER_USER


AUTHORITY_TIERS = {
    "tier1": {"founder", "co-founder", "ceo", "chief executive", "owner", "president"},
    "tier2": {"cto", "coo", "cfo", "cmo", "chief", "vp", "vice president", "director"},
    "tier3": {"head of", "manager", "lead", "principal"},
}
IDEAL_SIZE_KEYWORDS = {"1-10", "10-50", "11-50", "50-200", "51-200", "10-100", "1-50"}


def scorer_agent(state: OutreachState) -> dict:
    """LangGraph node: score verified leads and filter below threshold."""
    log_agent("ScorerAgent", "📊 Scoring & qualifying leads...", "info")

    verified = state.get("verified_leads") or []
    if not verified:
        raw_count = len(state.get("raw_leads") or [])
        if raw_count:
            log_agent(
                "ScorerAgent",
                f"No verified leads to score — verifier dropped all {raw_count} prospect(s)",
                "warn",
            )
        else:
            log_agent("ScorerAgent", "No leads to score", "warn")
        return {"filtered_leads": [], "scored_leads": []}

    leads = verified

    brief: SearchBrief = state.get("brief", {}) or {}
    threshold = settings.lead_score_threshold
    scored: list[EnrichedLead] = []
    use_llm = True

    for lead in leads:
        try:
            scored.append(_score_lead(lead, brief, use_llm=use_llm))
        except RateLimitExhausted:
            use_llm = False
            log_agent("ScorerAgent", "LLM rate-limited — switching to rule-based scoring", "warn")
            scored.append(_score_lead(lead, brief, use_llm=False))
        except Exception as e:
            log_agent("ScorerAgent", f"Scoring failed for {lead.get('name')}: {e}", "warn")
            scored.append(_score_lead(lead, brief, use_llm=False))

    filtered = [l for l in scored if l.get("score", 0) >= threshold]
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


def _score_lead(lead: Lead, brief: SearchBrief, *, use_llm: bool = True) -> EnrichedLead:
    rule_score = _rule_based_score(lead, brief)
    llm_data: dict = {}
    llm_score = rule_score

    if use_llm:
        prompt = SCORER_USER.format(
            goal=brief.get("goal", ""),
            offering_summary=brief.get("offering_summary", settings.default_offering_summary),
            roles=", ".join(brief.get("target_roles", [])),
            industries=", ".join(brief.get("target_industries", [])),
            name=lead.get("name", "Unknown"),
            title=lead.get("title", "Unknown"),
            company=lead.get("company", "Unknown"),
            industry=lead.get("industry", "Unknown"),
            company_size=lead.get("company_size", "Unknown"),
            location=lead.get("location", "Unknown"),
            snippet=(lead.get("snippet", "") or "")[:300],
        )
        response = invoke_smart_or_fast(
            [
                SystemMessage(content=SCORER_SYSTEM),
                HumanMessage(content=prompt),
            ],
            temperature=0.1,
            label="ScorerAgent",
            prefer_fast=True,
        )
        llm_data = safe_json_parse(response.content, fallback={}) or {}
        llm_score = int(llm_data.get("score", rule_score) or rule_score)

    # 55% LLM + 35% rules + up to 10% verification bonus (rules-only when no LLM)
    verify_conf = (lead.get("verification") or {}).get("confidence", 0) or 0
    verify_bonus = int(0.10 * verify_conf)
    if use_llm and llm_data:
        final = int(0.55 * llm_score + 0.35 * rule_score) + verify_bonus
    else:
        final = min(100, rule_score + verify_bonus)
    final = max(0, min(100, final))

    return {
        **lead,
        "score": final,
        "score_reasons": llm_data.get("reasons") or ["Rule-based score"],
        "recommended_service": llm_data.get("recommended_service") or "Custom solution",
        "best_channel": llm_data.get("best_channel") or _infer_channel(lead),
        "fit_reason": llm_data.get("fit_reason") or "",
    }


def _rule_based_score(lead: Lead, brief: SearchBrief) -> int:
    score = 0
    title = (lead.get("title") or "").lower()
    if any(k in title for k in AUTHORITY_TIERS["tier1"]):
        score += 30
    elif any(k in title for k in AUTHORITY_TIERS["tier2"]):
        score += 22
    elif any(k in title for k in AUTHORITY_TIERS["tier3"]):
        score += 14

    industry = (lead.get("industry") or "").lower()
    target_inds = [i.lower() for i in brief.get("target_industries", [])]
    if target_inds and any(ti in industry or industry in ti for ti in target_inds if ti):
        score += 25
    elif industry:
        score += 10

    size = (lead.get("company_size") or "").lower()
    if any(s in size for s in IDEAL_SIZE_KEYWORDS):
        score += 20
    elif size not in ("", "unknown"):
        score += 10

    if lead.get("email") and "@" in lead.get("email", ""):
        score += 15
    if lead.get("linkedin_url"):
        score += 10

    return min(score, 100)


def _infer_channel(lead: Lead) -> str:
    if lead.get("email") and "@" in lead.get("email", ""):
        return "email"
    if lead.get("linkedin_url"):
        return "linkedin"
    return "email"