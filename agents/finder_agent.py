"""
agents/finder_agent.py
----------------------
Agent 1 — Prospect Discovery (v2: web-search powered + CV-aware)

Discovers potential clients from multiple channels:
  - LinkedIn via Tavily web search (real public profiles)
  - Apify LinkedIn scraper (if API key set)
  - LLM-generated realistic leads (fallback / fill)

Targets 15-20 diverse personas who need Agentic AI/ML services:
  CEOs, CTOs, Founders, Operations Heads, Customer Service Directors,
  Marketing Directors, Sales VPs — across industries that benefit from AI.

Outputs: raw_leads appended to state
"""

from __future__ import annotations
import json
import re
from langchain_core.messages import SystemMessage, HumanMessage
from state.schema import OutreachState, Lead
from utils.helpers import log_agent, safe_json_parse, new_id, now_iso
from utils.llm import fast_llm, smart_llm
from tools.scraper import linkedin_scraper
from tools.search import search_tool
from config.settings import settings
from prompts.templates import FINDER_SYSTEM, FINDER_USER
from utils.cv_parser import get_cv_summary


# ── Diverse search queries for real lead discovery ────────────────────────────
LINKEDIN_SEARCH_QUERIES = [
    "CEO founder startup automate customer service operations site:linkedin.com",
    "CTO \"looking for\" AI automation developer startup site:linkedin.com",
    "founder e-commerce \"no AI team\" automate processes site:linkedin.com",
    "head of operations logistics SaaS manual processes AI site:linkedin.com",
    "CEO small business \"customer support\" \"chatbot\" OR \"automation\" site:linkedin.com",
    "founder healthcare OR legaltech OR fintech AI implementation site:linkedin.com",
    "VP Sales marketing agency \"lead generation\" automation AI site:linkedin.com",
    "startup founder \"we need\" AI developer chatbot RAG site:linkedin.com",
    "director engineering SaaS \"build AI\" OR \"automate\" workflow site:linkedin.com",
    "CEO real estate proptech AI OR automation OR chatbot site:linkedin.com",
]

WEB_SEARCH_QUERIES = [
    "startup CEO looking to automate customer service with AI 2024 2025",
    "SaaS founder hiring AI/ML developer for chatbot automation",
    "small business owner wants to implement AI chatbot customer service",
    "e-commerce founder automate inventory management AI",
    "logistics company CEO automate operations with AI",
    "healthcare startup founder AI implementation",
    "legaltech startup CTO AI document automation",
    "edtech founder personalized learning AI",
    "fintech CEO automate compliance reporting AI",
    "marketing agency director AI content automation tools",
]


def finder_agent(state: OutreachState) -> dict:
    """
    LangGraph node: discovers prospects from all configured channels.

    Strategy:
    1. Try LinkedIn via Apify (real data, requires API key)
    2. Web search via Tavily for LinkedIn profiles + prospect signals
    3. Fall back to LLM-generated realistic leads to fill quota
    4. Merge, deduplicate, cap at max_leads
    """
    log_agent("FinderAgent", "Starting prospect discovery (web + LinkedIn + LLM)...", "info")

    max_leads = state.get("max_leads", settings.max_leads_per_run)
    target_roles = state.get("target_roles", settings.target_roles)
    target_industries = state.get("target_industries", settings.target_industries)
    all_leads: list[Lead] = []

    # ── Step 1: LinkedIn scraping via Apify ──────────────────────────────────
    if settings.apify_api_key:
        try:
            log_agent("FinderAgent", "Attempting LinkedIn scrape via Apify...", "info")
            linkedin_leads = linkedin_scraper.search_leads(
                roles=target_roles[:5],
                industries=target_industries[:5],
                max_results=max_leads // 2,
            )
            if linkedin_leads:
                log_agent("FinderAgent", f"LinkedIn: found {len(linkedin_leads)} leads", "done")
                all_leads.extend(linkedin_leads)
            else:
                log_agent("FinderAgent", "LinkedIn scraper returned 0 leads", "warn")
        except Exception as e:
            log_agent("FinderAgent", f"LinkedIn scrape failed: {e}", "warn")

    # ── Step 2: Tavily web search for real prospect signals ───────────────────
    if settings.tavily_api_key and len(all_leads) < max_leads:
        log_agent("FinderAgent", "Running web searches to find prospect signals...", "info")
        web_leads = _find_leads_via_web_search(
            count=max(max_leads - len(all_leads), 8),
            roles=target_roles,
            industries=target_industries,
        )
        log_agent("FinderAgent", f"Web search: found {len(web_leads)} prospect signals", "done")
        all_leads.extend(web_leads)

    # ── Step 3: LLM-generated leads (fills remaining quota) ──────────────────
    remaining = max_leads - len(all_leads)
    if remaining > 0:
        log_agent("FinderAgent", f"Generating {remaining} additional leads via LLM...", "info")
        llm_leads = _generate_leads_via_llm(
            count=remaining,
            roles=target_roles,
            industries=target_industries,
        )
        log_agent("FinderAgent", f"LLM: generated {len(llm_leads)} leads", "done")
        all_leads.extend(llm_leads)

    # ── Step 4: Deduplicate on company+name ──────────────────────────────────
    seen = set()
    unique_leads = []
    for lead in all_leads:
        key = f"{lead.get('company', '').lower().strip()}:{lead.get('name', '').lower().strip()}"
        if key not in seen and lead.get("name"):
            seen.add(key)
            if not lead.get("id"):
                lead["id"] = new_id()
            unique_leads.append(lead)

    unique_leads = unique_leads[:max_leads]
    log_agent("FinderAgent", f"Discovery complete: {len(unique_leads)} unique leads", "done")

    return {
        "raw_leads": unique_leads,
        "current_agent": "finder",
        "logs": [log_agent("FinderAgent", f"Discovered {len(unique_leads)} prospects", "done")],
    }


def _find_leads_via_web_search(
    count: int,
    roles: list[str],
    industries: list[str],
) -> list[Lead]:
    """
    Use Tavily to find real prospect signals from LinkedIn and web.
    Extracts names, companies, and LinkedIn URLs from search snippets.
    """
    llm = smart_llm(temperature=0.3)
    leads: list[Lead] = []
    queries_to_run = LINKEDIN_SEARCH_QUERIES[:5] + WEB_SEARCH_QUERIES[:5]

    # Build combined search results
    all_results_text = []
    for query in queries_to_run[:8]:  # Limit to control API usage
        try:
            result = search_tool.search(query, max_results=3)
            if result and "Search failed" not in result and "unavailable" not in result:
                all_results_text.append(f"Query: {query}\n{result}")
        except Exception:
            continue

    if not all_results_text:
        return []

    combined_results = "\n\n---\n\n".join(all_results_text[:6])

    # Use LLM to extract structured lead data from search results
    extract_prompt = f"""You are analyzing web search results to find real people who are potential clients for an Agentic AI/ML developer.

Search results:
{combined_results[:5000]}

From these results, extract up to {count} real or realistic people who:
1. Are founders, CEOs, CTOs, Operations heads, VP Sales, or similar decision-makers
2. Work in industries that benefit from AI: SaaS, E-commerce, Healthcare, Logistics, Legal Tech, 
   Finance, HR Tech, EdTech, Real Estate, Marketing, Customer Service, Retail, InsurTech
3. Likely need: AI chatbots, automation, RAG systems, agentic AI workflows, or computer vision

For each person found (or inferred from company context), return structured JSON.
If a LinkedIn URL appears in the results, include it. Otherwise generate a plausible one.

Return a JSON array of up to {count} objects, each with EXACTLY these keys:
{{
  "id": "unique 8-char alphanumeric",
  "name": "Full Name",
  "first_name": "First Name",
  "title": "Job Title",
  "company": "Company Name",
  "company_website": "https://...",
  "linkedin_url": "https://linkedin.com/in/username",
  "email": "person@company.com",
  "location": "City, Country",
  "industry": "Industry",
  "company_size": "10-50",
  "source": "web_search"
}}

Be specific and realistic. Use actual company names from the results where possible.
Vary industries, locations (US, UK, EU, Middle East, Asia), and roles.
Return ONLY the JSON array, no other text."""

    try:
        response = llm.invoke([
            SystemMessage(content="You extract structured lead data from web search results. Return only valid JSON arrays."),
            HumanMessage(content=extract_prompt),
        ])
        leads_data = safe_json_parse(response.content, fallback=[])
        if isinstance(leads_data, list):
            for item in leads_data:
                if isinstance(item, dict) and item.get("name"):
                    lead: Lead = {
                        "id": item.get("id", new_id()),
                        "name": item.get("name", ""),
                        "first_name": item.get("first_name", item.get("name", "").split()[0] if item.get("name") else ""),
                        "title": item.get("title", ""),
                        "company": item.get("company", ""),
                        "company_website": item.get("company_website", ""),
                        "linkedin_url": item.get("linkedin_url", ""),
                        "email": item.get("email", ""),
                        "location": item.get("location", ""),
                        "industry": item.get("industry", ""),
                        "company_size": item.get("company_size", "unknown"),
                        "source": "web_search",
                    }
                    leads.append(lead)
    except Exception as e:
        log_agent("FinderAgent", f"Web search lead extraction failed: {e}", "warn")

    return leads


def _generate_leads_via_llm(
    count: int,
    roles: list[str],
    industries: list[str],
) -> list[Lead]:
    """
    Use the LLM to generate realistic, diverse lead profiles.
    Fallback when scraping APIs are unavailable or quota not met.
    """
    cv = get_cv_summary()
    services_offered = ", ".join([p["name"] for p in cv["projects"]])

    llm = smart_llm(temperature=0.8)

    prompt = f"""Generate {count} highly realistic B2B lead profiles for an Agentic AI/ML Developer named Shaheer.

Services Shaheer offers: {services_offered}

Target criteria:
- Industries: {", ".join(industries[:10])}
- Roles: {", ".join(roles[:8])}
- Company size: 5-500 employees (SMBs, not Fortune 500)
- Pain signs: manual processes, no internal AI team, customer support issues, data silos, 
  growth without automation, repetitive workflows that AI could handle

Make them DIVERSE:
- Mix of US, UK, EU, Middle East, Asia, Australia locations
- Mix of male/female names
- Mix of industries (NOT all from the same sector)
- Mix of company stages (early startup to established SMB)
- Include some with LinkedIn URLs, some with emails, some with both

Return a JSON array of exactly {count} objects with these EXACT keys:
{{
  "id": "unique 8-char alphanumeric string",
  "name": "Full Name",
  "first_name": "First Name",
  "title": "Job Title",
  "company": "Company Name",
  "company_website": "https://realdomainexample.com",
  "linkedin_url": "https://linkedin.com/in/username",
  "email": "person@company.com",
  "location": "City, Country",
  "industry": "Exact Industry",
  "company_size": "e.g. 10-50",
  "source": "llm_generated"
}}

Be VERY specific and realistic. Use believable company names and realistic emails.
Return ONLY the JSON array."""

    try:
        response = llm.invoke([
            SystemMessage(content=FINDER_SYSTEM),
            HumanMessage(content=prompt),
        ])
        leads_data = safe_json_parse(response.content, fallback=[])
        leads = []
        for item in leads_data:
            if isinstance(item, dict) and item.get("name"):
                lead: Lead = {
                    "id": item.get("id", new_id()),
                    "name": item.get("name", ""),
                    "first_name": item.get("first_name", item.get("name", "").split()[0] if item.get("name") else ""),
                    "title": item.get("title", ""),
                    "company": item.get("company", ""),
                    "company_website": item.get("company_website", ""),
                    "linkedin_url": item.get("linkedin_url", ""),
                    "email": item.get("email", ""),
                    "location": item.get("location", ""),
                    "industry": item.get("industry", ""),
                    "company_size": item.get("company_size", "unknown"),
                    "source": item.get("source", "llm_generated"),
                }
                leads.append(lead)
        return leads
    except Exception as e:
        log_agent("FinderAgent", f"LLM lead generation failed: {e}", "error")
        return []
