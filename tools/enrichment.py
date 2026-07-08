"""
tools/enrichment.py
-------------------
Tavily + Apollo helpers to fill gaps in sparse Apify/LinkedIn leads.
Apify 'Short' profiles often lack email, website, and stringify poorly — we
merge discovery sources and enrich before verification.
"""

from __future__ import annotations

import re
from typing import Any

from config.settings import settings
from tools.search import search_tool
from tools.verification import domain_from_url, is_valid_linkedin, normalize_url
from utils.helpers import coerce_text, log_agent

_NON_COMPANY_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "crunchbase.com", "youtube.com", "medium.com", "github.com", "wikipedia.org",
    "bloomberg.com", "glassdoor.com", "indeed.com", "pitchbook.com", "reddit.com",
    "apollo.io", "zoominfo.com", "wellfound.com", "angel.co", "producthunt.com",
}

_company_cache: dict[str, dict[str, str]] = {}


def normalize_lead_fields(lead: dict) -> dict:
    """Coerce Apify/Tavily fields to plain strings the UI and agents expect."""
    out = dict(lead)
    for key in (
        "name", "first_name", "title", "company", "company_website",
        "linkedin_url", "email", "location", "industry", "company_size",
        "source", "source_url", "snippet", "email_source",
    ):
        if key in out:
            out[key] = coerce_text(out.get(key))

    # Never treat the data source as an industry vertical
    if out.get("industry", "").lower() in {"linkedin", "apify", "tavily", "web", ""}:
        out["industry"] = ""

    if out.get("company", "").lower() in {"linkedin", "unknown", "n/a"}:
        out["company"] = ""

    if not out.get("first_name") and out.get("name"):
        out["first_name"] = out["name"].split()[0]

    return out


def _cache_key(company: str, name: str = "") -> str:
    return f"{company.strip().lower()}|{name.strip().lower()}"


def lookup_company_intel(company: str, person: str = "") -> dict[str, str]:
    """
    One Tavily search per company (cached) to recover website, industry, size hints.
    """
    company = coerce_text(company)
    if not company or not settings.tavily_api_key:
        return {}

    key = _cache_key(company, person)
    if key in _company_cache:
        return _company_cache[key]

    query = f'"{company}" official website company'
    if person:
        query = f'"{person}" "{company}" company website'
    results = search_tool.search_raw(query, max_results=5, depth="basic")

    intel: dict[str, str] = {"website": "", "industry": "", "company_size": "", "snippet": ""}
    for r in results:
        url = normalize_url(r.get("url", ""))
        domain = domain_from_url(url)
        base = ".".join(domain.split(".")[-2:]) if domain else ""
        content = r.get("content", "") or ""

        if domain and base and base not in _NON_COMPANY_DOMAINS and not intel["website"]:
            intel["website"] = f"https://{domain}"

        if not intel["snippet"] and content:
            intel["snippet"] = content[:400]

        if not intel["industry"]:
            for label in ("SaaS", "E-commerce", "Healthcare", "FinTech", "EdTech", "Marketing", "Retail"):
                if label.lower() in content.lower():
                    intel["industry"] = label
                    break

        m = re.search(r"(\d{1,4}\+?\s*employees|\d+-\d+\s*employees)", content, re.I)
        if m and not intel["company_size"]:
            intel["company_size"] = m.group(1)

    _company_cache[key] = intel
    return intel


def enrich_lead_record(lead: dict, *, allow_tavily: bool = True) -> dict:
    """Fill missing company website, industry, title, and snippet on a single lead."""
    lead = normalize_lead_fields(lead)
    company = lead.get("company", "")
    name = lead.get("name", "")

    if allow_tavily and settings.tavily_api_key and company:
        needs = not lead.get("company_website") or not lead.get("industry") or not lead.get("snippet")
        if needs:
            intel = lookup_company_intel(company, name)
            if intel.get("website") and not lead.get("company_website"):
                lead["company_website"] = intel["website"]
            if intel.get("industry") and not lead.get("industry"):
                lead["industry"] = intel["industry"]
            if intel.get("company_size") and not lead.get("company_size"):
                lead["company_size"] = intel["company_size"]
            if intel.get("snippet") and not lead.get("snippet"):
                lead["snippet"] = intel["snippet"]

    if not lead.get("title") and lead.get("snippet"):
        m = re.search(
            r"(founder|ceo|cto|co-founder|director|head of [a-z ]+|vp[a-z ]*)",
            lead["snippet"],
            re.I,
        )
        if m:
            lead["title"] = m.group(1).title()

    if not lead.get("company") and lead.get("linkedin_url") and is_valid_linkedin(lead["linkedin_url"]):
        # Last resort: don't leave company blank on LinkedIn-only rows
        lead["company"] = name or "Unknown company"

    return lead


def enrich_leads_batch(leads: list[dict], *, max_tavily_lookups: int = 8) -> list[dict]:
    """Enrich a list of leads with a Tavily budget cap."""
    out: list[dict] = []
    fresh_lookups = 0
    for lead in leads:
        lead = normalize_lead_fields(lead)
        company = lead.get("company", "")
        cache_key = _cache_key(company, lead.get("name", ""))
        needs = bool(company and (not lead.get("company_website") or not lead.get("industry")))
        allow = needs and (
            cache_key in _company_cache or fresh_lookups < max_tavily_lookups
        )
        if needs and cache_key not in _company_cache and fresh_lookups < max_tavily_lookups:
            fresh_lookups += 1
        out.append(enrich_lead_record(lead, allow_tavily=allow))
    return out
