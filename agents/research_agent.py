"""
agents/research_agent.py
------------------------
Agent 3 — Deep Lead Research

For each qualified lead, runs:
  1. Company web search (overview, news, tech stack)
  2. Pain point extraction
  3. Opportunity mapping to Shaheer's services
  4. Best project matching

Uses parallel processing where possible for speed.
"""

from __future__ import annotations
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from langchain_core.messages import SystemMessage, HumanMessage
from state.schema import OutreachState, EnrichedLead
from utils.helpers import log_agent, safe_json_parse, now_iso
from utils.llm import fast_llm, smart_llm
from tools.search import search_tool
from tools.scraper import apollo_enricher
from config.settings import settings
from prompts.templates import (
    RESEARCH_SYSTEM, RESEARCH_USER,
    PROJECT_MATCH_SYSTEM, PROJECT_MATCH_USER,
)


def research_agent(state: OutreachState) -> dict:
    """
    LangGraph node: researches each filtered lead in parallel.
    Adds company_summary, tech_stack, pain_points, opportunities,
    project_reference to each EnrichedLead.
    """
    log_agent("ResearchAgent", "🔬 Deep researching qualified leads...", "info")

    filtered_leads = state.get("filtered_leads", [])
    if not filtered_leads:
        log_agent("ResearchAgent", "No leads to research", "warn")
        return {"researched_leads": []}

    # Parallel research — up to 3 concurrent to avoid API rate limits
    researched: list[EnrichedLead] = []

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(_research_lead, lead): lead
            for lead in filtered_leads
        }
        for future in as_completed(futures):
            original_lead = futures[future]
            try:
                result = future.result()
                researched.append(result)
                log_agent(
                    "ResearchAgent",
                    f"✓ Researched: {result.get('company')} — {len(result.get('pain_points', []))} pain points found",
                    "done",
                )
            except Exception as e:
                log_agent(
                    "ResearchAgent",
                    f"Research failed for {original_lead.get('company')}: {e}",
                    "warn",
                )
                # Add lead without research rather than lose it
                researched.append({**original_lead, "pain_points": [], "opportunities": []})

    log_agent("ResearchAgent", f"✓ Research complete: {len(researched)} leads enriched", "done")

    return {
        "researched_leads": researched,
        "current_agent": "research",
        "logs": [log_agent("ResearchAgent", f"Researched {len(researched)} leads", "done")],
    }


def _research_lead(lead: EnrichedLead) -> EnrichedLead:
    """Full research pipeline for a single lead."""
    company = lead.get("company", "")
    website = lead.get("company_website", "")
    industry = lead.get("industry", "")
    name = lead.get("name", "")

    # ── Step 1: Apollo enrichment (email + LinkedIn URL) ────────────────────
    if not lead.get("email") or not lead.get("linkedin_url"):
        enrichment = apollo_enricher.enrich_lead(name, company, website)
        if enrichment:
            lead = {**lead, **{k: v for k, v in enrichment.items() if v and not lead.get(k)}}

    # ── Step 2: Web research ─────────────────────────────────────────────────
    search_results = search_tool.search_company(company, website)

    # ── Step 3: LLM extracts structured intel ───────────────────────────────
    llm = smart_llm(temperature=0.2)

    research_prompt = RESEARCH_USER.format(
        company=company,
        website=website,
        industry=industry,
        search_results=search_results[:3000],  # Truncate to fit context
    )

    messages = [
        SystemMessage(content=RESEARCH_SYSTEM),
        HumanMessage(content=research_prompt),
    ]

    response = llm.invoke(messages)
    research_data = safe_json_parse(response.content, fallback={})

    # ── Step 4: Match best project ───────────────────────────────────────────
    pain_points = research_data.get("pain_points", [])
    opportunities = research_data.get("opportunities", [])
    recommended_service = lead.get("recommended_service", "AI Automation")

    project_match = _match_project(pain_points, industry, recommended_service)

    # ── Step 5: Merge everything ─────────────────────────────────────────────
    enriched: EnrichedLead = {
        **lead,
        "company_summary": research_data.get("company_summary", f"{company} is a {industry} company."),
        "recent_news": research_data.get("recent_news", "No recent news found."),
        "tech_stack": research_data.get("tech_stack", []),
        "pain_points": pain_points,
        "opportunities": opportunities,
        "competitor_context": research_data.get("competitor_context", ""),
        "project_reference": project_match.get("project_name", "NexusIQ"),
        # Store full project match for Writer agent
        "_project_match": project_match,
    }

    return enriched


def _match_project(
    pain_points: list[str],
    industry: str,
    recommended_service: str,
) -> dict:
    """
    Match Shaheer's best project to this lead's pain points.
    Uses the fast LLM — it's a classification task.
    """
    llm = fast_llm(temperature=0.1)

    projects_json = json.dumps(settings.projects, indent=2)

    prompt = PROJECT_MATCH_USER.format(
        pain_points=", ".join(pain_points[:3]) if pain_points else "general AI automation",
        industry=industry,
        recommended_service=recommended_service,
        projects_json=projects_json,
    )

    messages = [
        SystemMessage(content=PROJECT_MATCH_SYSTEM),
        HumanMessage(content=prompt),
    ]

    response = llm.invoke(messages)
    match_data = safe_json_parse(response.content, fallback={})

    # Fallback if LLM fails
    if not match_data.get("project_name"):
        match_data = {
            "project_name": "NexusIQ",
            "project_description": "Enterprise RAG knowledge agent",
            "value_proposition": "Automate knowledge retrieval and reduce manual lookup time by 80%",
            "proof_point": "Sub-2-second end-to-end latency",
        }

    return match_data
