"""
agents/research_agent.py
------------------------
Agent 3 — Deep Lead Research

For each qualified lead, runs:
  1. Company web search (overview, news, tech stack)
  2. Pain point extraction
  3. Opportunity mapping to Shaheer's services
  4. Best project matching

Runs sequentially and prefers the fast Groq model so free-tier TPM/TPD
limits don't stall the whole pipeline.
"""

from __future__ import annotations
import json
import re
import time
from langchain_core.messages import SystemMessage, HumanMessage
from state.schema import OutreachState, EnrichedLead
from utils.helpers import log_agent, safe_json_parse
from utils.llm import invoke_smart_or_fast, RateLimitExhausted
from tools.search import search_tool
from config.settings import settings
from prompts.templates import (
    RESEARCH_SYSTEM, RESEARCH_USER,
)


def research_agent(state: OutreachState) -> dict:
    """
    LangGraph node: researches each filtered lead sequentially.
    Adds company_summary, tech_stack, pain_points, opportunities,
    project_reference to each EnrichedLead.
    """
    log_agent("ResearchAgent", "🔬 Deep researching qualified leads...", "info")

    filtered_leads = state.get("filtered_leads", [])
    if not filtered_leads:
        log_agent("ResearchAgent", "No leads to research", "warn")
        return {"researched_leads": []}

    brief = state.get("brief", {}) or {}
    researched: list[EnrichedLead] = []

    # Sequential — parallel LLM calls burn free-tier TPM and cause endless 429s
    for i, lead in enumerate(filtered_leads):
        try:
            result = _research_lead(lead, brief)
            researched.append(result)
            log_agent(
                "ResearchAgent",
                f"✓ Researched: {result.get('company')} — "
                f"{len(result.get('pain_points', []))} pain points found",
                "done",
            )
        except Exception as e:
            log_agent(
                "ResearchAgent",
                f"Research failed for {lead.get('company')}: {e} — using heuristic fallback",
                "warn",
            )
            researched.append(_heuristic_research(lead, brief))

        # Small pause between leads to stay under TPM
        if i < len(filtered_leads) - 1:
            time.sleep(0.8)

    log_agent("ResearchAgent", f"✓ Research complete: {len(researched)} leads enriched", "done")

    return {
        "researched_leads": researched,
        "current_agent": "research",
        "logs": [log_agent("ResearchAgent", f"Researched {len(researched)} leads", "done")],
    }


def _research_lead(lead: EnrichedLead, brief: dict) -> EnrichedLead:
    """Full research pipeline for a single lead."""
    company = lead.get("company", "")
    website = lead.get("company_website", "")
    industry = lead.get("industry", "")

    offering_summary = brief.get("offering_summary", settings.default_offering_summary)
    goal = brief.get("goal", "")

    search_results = search_tool.search_company(company, website)

    research_prompt = RESEARCH_USER.format(
        company=company,
        website=website,
        industry=industry,
        offering_summary=offering_summary,
        goal=goal,
        search_results=search_results[:2200],
    )

    messages = [
        SystemMessage(content=RESEARCH_SYSTEM),
        HumanMessage(content=research_prompt),
    ]

    research_data: dict = {}
    try:
        # Prefer fast model — research is structured extraction, not creative writing
        response = invoke_smart_or_fast(
            messages,
            temperature=0.2,
            label="ResearchAgent",
            prefer_fast=True,
        )
        research_data = safe_json_parse(response.content, fallback={}) or {}
    except RateLimitExhausted:
        log_agent("ResearchAgent", f"LLM unavailable for {company} — heuristic research", "warn")
        return _heuristic_research(lead, brief, search_results)
    except Exception as e:
        log_agent("ResearchAgent", f"LLM research error for {company}: {e}", "warn")
        return _heuristic_research(lead, brief, search_results)

    if not isinstance(research_data, dict) or not research_data:
        return _heuristic_research(lead, brief, search_results)

    pain_points = research_data.get("pain_points") or []
    if isinstance(pain_points, str):
        pain_points = [p.strip() for p in pain_points.split(";") if p.strip()]
    opportunities = research_data.get("opportunities") or []
    if isinstance(opportunities, str):
        opportunities = [o.strip() for o in opportunities.split(";") if o.strip()]

    # Heuristic project match — avoids a second LLM call per lead
    project_match = _match_project_heuristic(
        pain_points, industry, brief.get("offering", settings.default_offering)
    )

    enriched: EnrichedLead = {
        **lead,
        "company_summary": research_data.get("company_summary")
            or f"{company} is a {industry or 'technology'} company.",
        "recent_news": research_data.get("recent_news") or "No recent news found.",
        "tech_stack": research_data.get("tech_stack") or [],
        "pain_points": pain_points,
        "opportunities": opportunities,
        "competitor_context": research_data.get("competitor_context") or "",
        "project_reference": project_match.get("project_name", "NexusIQ"),
        "_project_match": project_match,
    }
    return enriched


def _heuristic_research(lead: EnrichedLead, brief: dict, search_results: str = "") -> EnrichedLead:
    """Build usable research without an LLM when rate-limited."""
    company = lead.get("company", "") or "the company"
    industry = lead.get("industry", "") or "technology"
    title = lead.get("title", "") or "decision-maker"
    offering = brief.get("offering_summary", settings.default_offering_summary)

    pain_points = [
        f"Manual processes slowing {industry} operations",
        f"Need for scalable automation as {company} grows",
    ]
    # Pull a couple of phrases from search snippets if present
    if search_results:
        for m in re.finditer(r"(?:challenge|struggle|need|looking for|pain)[^.!?\n]{10,80}", search_results, re.I):
            pain_points.append(m.group(0).strip())
            if len(pain_points) >= 3:
                break

    opportunities = [
        f"Help {title} at {company} automate repetitive workflows",
        f"Apply AI to improve {industry} efficiency",
    ]
    project_match = _match_project_heuristic(pain_points, industry, offering)

    return {
        **lead,
        "company_summary": f"{company} operates in {industry}.",
        "recent_news": "No recent news found.",
        "tech_stack": [],
        "pain_points": pain_points[:3],
        "opportunities": opportunities[:2],
        "competitor_context": "",
        "project_reference": project_match.get("project_name", "NexusIQ"),
        "_project_match": project_match,
    }


def _match_project_heuristic(pain_points: list[str], industry: str, offering: str) -> dict:
    """Pick the best project from settings without an LLM call."""
    projects = settings.projects or []
    if not projects:
        return {
            "project_name": "Custom solution",
            "project_description": "A tailored solution mapped to their workflow",
            "value_proposition": "Cut manual work and ship measurable results fast",
            "proof_point": "Proven, production-grade delivery",
        }

    blob = " ".join(pain_points + [industry or "", offering or ""]).lower()
    best = projects[0]
    best_score = -1
    for p in projects:
        score = 0
        for kw in p.get("best_for", []):
            if kw.lower() in blob:
                score += 2
        name = (p.get("name") or "").lower()
        if name and name in blob:
            score += 1
        if score > best_score:
            best_score = score
            best = p

    return {
        "project_name": best.get("name", "Custom solution"),
        "project_description": best.get("description", ""),
        "value_proposition": f"Use {best.get('name', 'our solution')} to address their workflow gaps",
        "proof_point": best.get("proof", "Proven, production-grade delivery"),
    }
