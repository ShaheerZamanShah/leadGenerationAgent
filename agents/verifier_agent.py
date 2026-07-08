"""
agents/verifier_agent.py
------------------------
Agent 2 — Lead Verification (proves leads are real)

For each discovered lead, runs concrete checks (in parallel):
  - Company website is live (DNS + HTTP)
  - Email is deliverable (valid syntax + domain MX record)
  - LinkedIn URL is a well-formed public profile

When an email is missing but Apollo is configured, tries to enrich it;
otherwise, if the company domain is live, proposes a pattern-guess email
(clearly labelled as a guess, never presented as verified).

Each lead gets a `verification` block and a `verified` boolean. If
STRICT_VERIFICATION is on, unverified leads are dropped.

Outputs: verified_leads written to state.
"""

from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed

from state.schema import OutreachState, EnrichedLead
from utils.helpers import log_agent
from tools.verification import (
    verify_lead, domain_from_url, guess_email_patterns,
    is_valid_email_syntax, has_mx_record, is_domain_live, normalize_url,
)
from tools.scraper import apollo_enricher
from tools.search import search_tool
from config.settings import settings


# Domains that are never a company's own website
_NON_COMPANY_DOMAINS = {
    "linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "crunchbase.com", "youtube.com", "medium.com", "github.com", "wikipedia.org",
    "bloomberg.com", "glassdoor.com", "indeed.com", "pitchbook.com", "reddit.com",
    "apollo.io", "zoominfo.com", "wellfound.com", "angel.co", "producthunt.com",
}


def _discover_company_domain(company: str) -> str:
    """Find a company's real, live website via web search (budget-limited)."""
    global _domain_lookups_used
    if not company or not settings.tavily_api_key or _domain_lookups_used >= _DOMAIN_LOOKUP_BUDGET:
        return ""
    _domain_lookups_used += 1
    results = search_tool.search_raw(f"{company} official website", max_results=5)
    for r in results:
        url = normalize_url(r.get("url", ""))
        domain = domain_from_url(url)
        if not domain:
            continue
        base = ".".join(domain.split(".")[-2:])
        if base in _NON_COMPANY_DOMAINS:
            continue
        # Prefer a domain that looks related to the company name
        if is_domain_live(url):
            return f"https://{domain}"
    return ""


# Cap expensive Tavily domain lookups during verification (Finder already searched)
_DOMAIN_LOOKUP_BUDGET = 4
_domain_lookups_used = 0


def verifier_agent(state: OutreachState) -> dict:
    """LangGraph node: verify each raw lead and label it real/partial/unverified."""
    log_agent("VerifierAgent", "🛡️  Verifying leads (domains, emails, LinkedIn)...", "info")

    raw_leads = state.get("raw_leads", []) or []
    if not raw_leads:
        log_agent("VerifierAgent", "No leads to verify", "warn")
        return {"verified_leads": []}

    verified: list[EnrichedLead] = []
    global _domain_lookups_used
    _domain_lookups_used = 0
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {executor.submit(_verify_one, dict(lead)): lead for lead in raw_leads}
        for future in as_completed(futures):
            original = futures[future]
            try:
                verified.append(future.result(timeout=60))
            except Exception as e:
                log_agent("VerifierAgent", f"Verify failed for {original.get('name')}: {e}", "warn")
                verified.append({**original, "verified": False,
                                 "verification": {"status": "unverified", "confidence": 0}})

    # Optionally drop unverified
    kept = verified
    if settings.strict_verification:
        kept = [l for l in verified if l.get("verified")]
        dropped = len(verified) - len(kept)
        if dropped:
            log_agent("VerifierAgent", f"Strict mode: dropped {dropped} unverified leads", "info")

    # Sort by verification confidence (best first)
    kept.sort(key=lambda l: l.get("verification", {}).get("confidence", 0), reverse=True)

    n_verified = sum(1 for l in kept if l.get("verification", {}).get("status") == "verified")
    n_partial = sum(1 for l in kept if l.get("verification", {}).get("status") == "partial")
    log_agent(
        "VerifierAgent",
        f"✓ Verification complete: {n_verified}/{len(kept)} leads verified, {n_partial} partial",
        "done",
    )
    return {
        "verified_leads": kept,
        "current_agent": "verifier",
        "logs": [log_agent("VerifierAgent", f"{n_verified}/{len(kept)} leads verified", "done")],
    }


def _verify_one(lead: dict) -> EnrichedLead:
    """Enrich (email) then verify a single lead."""
    # ── Try Apollo enrichment when email/linkedin is missing ─────────────────
    if apollo_enricher.available and (not lead.get("email") or not lead.get("linkedin_url")):
        enrich = apollo_enricher.enrich_lead(
            lead.get("name", ""), lead.get("company", ""), lead.get("company_website", "")
        )
        for k, v in enrich.items():
            if v and not lead.get(k):
                lead[k] = v

    # ── Discover a real company website if we don't have one ─────────────────
    # Skip when LinkedIn is present — domain lookup is slow and Finder already searched.
    if not lead.get("company_website") and lead.get("company") and not lead.get("linkedin_url"):
        found = _discover_company_domain(lead["company"])
        if found:
            lead["company_website"] = found

    # ── Pattern-guess an email if we have a real live domain but no email ────
    if not lead.get("email"):
        domain = domain_from_url(lead.get("company_website", ""))
        if domain:
            parts = (lead.get("name") or "").split()
            first = parts[0] if parts else ""
            last = " ".join(parts[1:]) if len(parts) > 1 else ""
            for candidate in guess_email_patterns(first, last, domain):
                if is_valid_email_syntax(candidate) and has_mx_record(domain):
                    lead["email"] = candidate
                    lead["email_source"] = "pattern-guess"
                    break

    verification = verify_lead(lead)
    lead["verification"] = verification
    lead["verified"] = verification["status"] in ("verified", "partial")
    return lead
