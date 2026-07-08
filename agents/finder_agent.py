"""
agents/finder_agent.py
----------------------
Agent 1 — Prospect Discovery (real leads only)

Discovers REAL potential clients based on the Planner's brief:
  1. Apify LinkedIn people search (real profiles) — if APIFY_API_KEY set
  2. Tavily web search over the brief's queries, then LLM extraction of the
     real people/companies that actually appear in the results

There is NO "invent fake people" fallback. If the web yields nothing, the
finder returns an empty list and the pipeline ends gracefully.

Outputs: raw_leads appended to state.
"""

from __future__ import annotations
import re
from langchain_core.messages import SystemMessage, HumanMessage
from state.schema import OutreachState, Lead, SearchBrief
from utils.helpers import log_agent, safe_json_parse, new_id
from utils.llm import invoke_smart_or_fast
from tools.scraper import linkedin_scraper
from tools.search import search_tool
from tools.verification import is_valid_linkedin, normalize_url, domain_from_url
from tools.enrichment import enrich_leads_batch, normalize_lead_fields
from config.settings import settings
from prompts.templates import FINDER_EXTRACT_SYSTEM, FINDER_EXTRACT_USER


def finder_agent(state: OutreachState) -> dict:
    """LangGraph node: discover real prospects from LinkedIn + web search."""
    preloaded = state.get("raw_leads") or []
    if state.get("skip_discovery") or preloaded:
        log_agent(
            "FinderAgent",
            f"Skipping discovery — using {len(preloaded)} pre-loaded lead(s)",
            "info",
        )
        return {"current_agent": "finder"}

    log_agent("FinderAgent", "🔎 Discovering real prospects (LinkedIn + web)...", "info")

    brief: SearchBrief = state.get("brief", {}) or {}
    max_leads = state.get("max_leads", settings.max_leads_per_run)
    queries = brief.get("search_queries", []) or []
    all_leads: list[Lead] = []

    # ── Step 1: Apify LinkedIn people search (real profiles) ──────────────────
    if settings.apify_api_key and queries:
        try:
            log_agent("FinderAgent", "Querying LinkedIn via Apify...", "info")
            li_leads = linkedin_scraper.search_leads(queries, max_results=max_leads)
            if li_leads:
                log_agent("FinderAgent", f"Apify: {len(li_leads)} real profiles", "done")
                all_leads.extend(li_leads)
            else:
                log_agent("FinderAgent", "Apify returned no profiles — continuing with web search", "warn")
        except Exception as e:
            log_agent("FinderAgent", f"Apify search skipped: {e}", "warn")

    # ── Step 2: Tavily web search — always run to supplement AND diversify ───
    # Apify alone often returns sparse LinkedIn rows (no email/website). Tavily
    # adds company pages and extra profiles even when Apify already hit max_leads.
    if settings.tavily_api_key:
        need = max(0, max_leads - len(all_leads))
        if need > 0:
            web_leads = _find_leads_via_web_search(brief, count=need)
            log_agent("FinderAgent", f"Web search: extracted {len(web_leads)} real leads", "done")
            all_leads.extend(web_leads)
        else:
            log_agent("FinderAgent", "Apify filled quota — running Tavily merge pass for enrichment", "info")

    # ── Step 3: Deduplicate, enrich sparse rows, cap ─────────────────────────
    unique = _dedupe(all_leads)[:max_leads]
    if settings.tavily_api_key and unique:
        unique = enrich_leads_batch(unique, max_tavily_lookups=min(8, len(unique)))
        log_agent("FinderAgent", "Tavily enrichment pass complete", "info")

    if not unique:
        log_agent(
            "FinderAgent",
            "No real prospects found for this brief. Try a broader or more specific prompt.",
            "warn",
        )

    log_agent("FinderAgent", f"✓ Discovery complete: {len(unique)} real leads", "done")
    return {
        "raw_leads": unique,
        "current_agent": "finder",
        "logs": [log_agent("FinderAgent", f"Discovered {len(unique)} real prospects", "done")],
    }


def _find_leads_via_web_search(brief: SearchBrief, count: int) -> list[Lead]:
    """Run the brief's search queries on Tavily and extract REAL leads."""
    queries = brief.get("search_queries", []) or []
    # Always include LinkedIn-focused queries so heuristic extraction has anchors
    roles = (brief.get("target_roles") or ["Founder", "CEO"])[:3]
    industries = (brief.get("target_industries") or ["SaaS"])[:2]
    locations = (brief.get("locations") or ["Europe"])[:2]
    extra = [
        f'site:linkedin.com/in "{" OR ".join(roles)}" {" OR ".join(industries)} {" OR ".join(locations)}',
        f'{" ".join(roles[:2])} {" ".join(industries[:1])} startup linkedin',
    ]
    all_queries = list(dict.fromkeys(list(queries[:8]) + extra))

    results: list[dict] = []
    seen_urls: set[str] = set()

    for q in all_queries:
        for r in search_tool.search_raw(q, max_results=5, depth="basic"):
            url = r.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                results.append(r)

    if not results:
        return []

    # Build a compact evidence block for the extractor
    blocks = []
    for r in results[:24]:
        blocks.append(
            f"- title: {r.get('title', '')}\n  url: {r.get('url', '')}\n  snippet: {r.get('content', '')[:300]}"
        )
    results_text = "\n".join(blocks)

    leads: list[Lead] = []
    try:
        response = invoke_smart_or_fast(
            [
                SystemMessage(content=FINDER_EXTRACT_SYSTEM),
                HumanMessage(content=FINDER_EXTRACT_USER.format(
                    goal=brief.get("goal", ""),
                    roles=", ".join(brief.get("target_roles", [])),
                    industries=", ".join(brief.get("target_industries", [])),
                    locations=", ".join(brief.get("locations", [])),
                    results=results_text[:4500],
                    count=count,
                )),
            ],
            temperature=0.2,
            label="FinderAgent",
            prefer_fast=True,
        )
        data = safe_json_parse(response.content, fallback=[])
        if isinstance(data, list):
            for item in data:
                lead = _sanitize_extracted(item)
                if lead:
                    leads.append(lead)
    except Exception as e:
        log_agent("FinderAgent", f"LLM extraction failed ({e}) — using heuristic extractor", "warn")

    if len(leads) < count:
        heuristic = _heuristic_extract_leads(results, brief, count=count)
        log_agent("FinderAgent", f"Heuristic extractor found {len(heuristic)} leads", "info")
        leads.extend(heuristic)

    return _dedupe(leads)[:count]


def _heuristic_extract_leads(results: list[dict], brief: SearchBrief, count: int) -> list[Lead]:
    """
    Extract real leads from search results without an LLM.
    Prefers LinkedIn profile URLs; also maps company pages with a role from the title.
    """
    roles = [r.lower() for r in (brief.get("target_roles") or [])]
    industries = brief.get("target_industries") or []
    industry = industries[0] if industries else ""
    leads: list[Lead] = []

    linkedin_re = re.compile(
        r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9_\-%]+/?",
        re.I,
    )
    # "Jane Doe - Founder at Acme" / "Jane Doe | CEO @ Acme"
    title_re = re.compile(
        r"^([A-Z][a-zA-Z.'\-]+(?:\s+[A-Z][a-zA-Z.'\-]+){0,3})\s*[-–|]\s*(.+?)(?:\s+(?:at|@|·)\s+(.+))?$",
    )

    for r in results:
        url = (r.get("url") or "").strip()
        title = (r.get("title") or "").strip()
        snippet = (r.get("content") or "").strip()
        blob = f"{title}\n{snippet}\n{url}"

        li_match = linkedin_re.search(blob)
        linkedin = li_match.group(0).rstrip("/") if li_match else ""
        if linkedin and not is_valid_linkedin(linkedin):
            linkedin = ""

        name = ""
        role = ""
        company = ""
        m = title_re.match(title.replace(" | LinkedIn", "").replace(" - LinkedIn", ""))
        if m:
            name = m.group(1).strip()
            role = (m.group(2) or "").strip()
            company = (m.group(3) or "").strip()

        if not name and linkedin:
            # slug → rough name: jane-doe-123 → Jane Doe
            slug = linkedin.rstrip("/").split("/")[-1]
            parts = [p for p in re.split(r"[-_]", slug) if p and not p.isdigit()]
            if 1 <= len(parts) <= 4:
                name = " ".join(p.capitalize() for p in parts[:3])

        website = ""
        if "linkedin.com" not in url.lower():
            website = normalize_url(url)

        # Need a real anchor
        if not linkedin and not domain_from_url(website):
            continue
        if not name and not company:
            # Company-only page
            company = title.split("|")[0].split("-")[0].strip()[:80]
            if not company:
                continue
            name = company
            role = roles[0].title() if roles else "Founder"

        # Soft role filter when we have role text
        if role and roles and not any(rr in role.lower() for rr in roles):
            # still keep LinkedIn profiles — titles are often messy
            if not linkedin:
                continue

        item = {
            "name": name or company,
            "first_name": (name.split()[0] if name else ""),
            "title": role or (roles[0].title() if roles else ""),
            "company": company or name,
            "company_website": website,
            "linkedin_url": linkedin,
            "email": "",
            "location": "",
            "industry": industry,
            "company_size": "",
            "source_url": url,
            "snippet": snippet[:400],
        }
        lead = _sanitize_extracted(item)
        if lead:
            leads.append(lead)
        if len(leads) >= count:
            break

    return leads


def _sanitize_extracted(item: dict) -> Lead | None:
    """Turn one extracted object into a clean Lead, dropping fabricated data."""
    if not isinstance(item, dict):
        return None
    name = (item.get("name") or "").strip()
    company = (item.get("company") or "").strip()
    if not name and not company:
        return None

    linkedin = (item.get("linkedin_url") or "").strip()
    if linkedin and not is_valid_linkedin(linkedin):
        # Keep only genuine profile URLs; discard invented ones
        linkedin = ""

    website = normalize_url(item.get("company_website") or "")

    # A real lead needs at least one anchor: a real LinkedIn URL or a company domain
    if not linkedin and not domain_from_url(website):
        return None

    first = (item.get("first_name") or (name.split()[0] if name else "")).strip()
    lead = {
        "id": new_id(),
        "name": name or company,
        "first_name": first,
        "title": (item.get("title") or "").strip(),
        "company": company,
        "company_website": website,
        "linkedin_url": linkedin,
        "email": (item.get("email") or "").strip(),
        "location": (item.get("location") or "").strip(),
        "industry": (item.get("industry") or "").strip(),
        "company_size": str(item.get("company_size") or "").strip(),
        "source": item.get("source") or "tavily",
        "source_url": (item.get("source_url") or "").strip(),
        "snippet": (item.get("snippet") or "").strip()[:400],
    }
    return normalize_lead_fields(lead)


def _dedupe(leads: list[Lead]) -> list[Lead]:
    seen: set[str] = set()
    out: list[Lead] = []
    for lead in leads:
        key = (
            (lead.get("linkedin_url") or "").lower().strip()
            or f"{(lead.get('company') or '').lower().strip()}:{(lead.get('name') or '').lower().strip()}"
        )
        if key and key not in seen:
            seen.add(key)
            if not lead.get("id"):
                lead["id"] = new_id()
            out.append(lead)
    return out
