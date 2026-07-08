"""
tools/scraper.py
----------------
Real lead-data providers:
  - LinkedInScraper : Apify actor for LinkedIn people search (real profiles)
  - ApolloEnricher  : Apollo.io People API for verified work emails

Both fall back gracefully (return empty) when their API key is missing,
so the pipeline keeps working on Tavily-sourced leads alone.
"""

from __future__ import annotations
import threading
from typing import Optional
from utils.helpers import log_agent, new_id, coerce_text

try:
    from apify_client import ApifyClient
    APIFY_AVAILABLE = True
except ImportError:
    APIFY_AVAILABLE = False

from config.settings import settings

# Once Apify free-tier is exhausted, skip it for the rest of the process lifetime
# so Finder doesn't burn minutes waiting on doomed actor runs.
_apify_disabled = False
_apify_lock = threading.Lock()


def _disable_apify(reason: str) -> None:
    global _apify_disabled
    with _apify_lock:
        if not _apify_disabled:
            _apify_disabled = True
            log_agent("LinkedInScraper", f"Disabling Apify for this session: {reason}", "warn")


class LinkedInScraper:
    """
    Searches LinkedIn for real people matching a brief using an Apify actor.
    Actor is configurable via APIFY_LINKEDIN_ACTOR (default:
    harvestapi/linkedin-profile-search).
    """

    def __init__(self):
        self.actor = settings.apify_linkedin_actor
        self.client = (
            ApifyClient(settings.apify_api_key)
            if APIFY_AVAILABLE and settings.apify_api_key
            else None
        )

    def search_leads(
        self,
        queries: list[str],
        max_results: int = 15,
    ) -> list[dict]:
        """
        Run people searches for each query string and return real leads.
        Returns [] on any failure so the pipeline can continue on Tavily data.
        """
        global _apify_disabled
        if not self.client or _apify_disabled:
            return []

        leads: list[dict] = []
        # One query only — free tier is tiny; multiple calls just burn wait time
        for query in queries[:1]:
            try:
                run_input = {
                    "searchQuery": query,
                    "profileScraperMode": "Short",
                    "maxItems": min(max_results, 10),
                }
                from datetime import timedelta
                # Hard timeout so a stuck/limited actor cannot freeze Finder
                run = self.client.actor(self.actor).call(
                    run_input=run_input,
                    run_timeout=timedelta(seconds=45),
                    wait_duration=timedelta(seconds=45),
                )
                if run is None:
                    continue

                status_msg = ""
                if isinstance(run, dict):
                    dataset_id = run.get("defaultDatasetId")
                    status_msg = str(run.get("statusMessage") or run.get("status") or "")
                else:
                    dataset_id = getattr(run, "default_dataset_id", None)
                    status_msg = str(
                        getattr(run, "status_message", None)
                        or getattr(run, "status", None)
                        or ""
                    )

                if "free user" in status_msg.lower() or "limit reached" in status_msg.lower():
                    _disable_apify(status_msg or "free tier limit reached")
                    return leads[:max_results]

                if not dataset_id:
                    continue
                items = list(self.client.dataset(dataset_id).iterate_items())
                if not items:
                    # Empty result after a "succeeded" free-limit run
                    if "limit" in status_msg.lower():
                        _disable_apify(status_msg)
                        return []
                for item in items:
                    lead = self._map_item(item)
                    if lead:
                        leads.append(lead)
            except Exception as e:
                msg = str(e).lower()
                if "limit" in msg or "quota" in msg or "payment" in msg:
                    _disable_apify(str(e))
                else:
                    log_agent("LinkedInScraper", f"Apify search failed: {e}", "warn")
                break

        return leads[:max_results]

    def _map_item(self, item: dict) -> dict | None:
        """Map an Apify result to our Lead schema (tolerant of field names/nulls)."""
        name = (
            item.get("fullName") or item.get("name")
            or f"{item.get('firstName') or ''} {item.get('lastName') or ''}".strip()
        )
        if not name:
            return None

        profile = (
            item.get("profileUrl") or item.get("linkedinUrl") or item.get("url") or ""
        )

        # harvestapi / similar actors nest company + role under currentPosition(s)
        company = coerce_text(item.get("companyName") or item.get("company") or "")
        title = coerce_text(item.get("headline") or item.get("title") or item.get("occupation") or "")
        positions = item.get("currentPositions") or item.get("currentPosition") or []
        if isinstance(positions, dict):
            positions = [positions]
        if positions and isinstance(positions, list):
            pos = positions[0] if positions else {}
            if isinstance(pos, dict):
                company = company or coerce_text(pos.get("companyName") or pos.get("company"))
                title = title or coerce_text(pos.get("title") or pos.get("position"))

        website = coerce_text(
            item.get("companyWebsite") or item.get("companyUrl")
            or item.get("companyWebsiteUrl") or ""
        )

        return {
            "id": new_id(),
            "name": coerce_text(name),
            "first_name": coerce_text(item.get("firstName") or (name.split()[0] if name else "")),
            "title": title,
            "company": company,
            "company_website": website,
            "linkedin_url": coerce_text(profile),
            "email": coerce_text(item.get("email") or ""),
            "location": coerce_text(item.get("location") or item.get("addressWithCountry") or item.get("geo") or ""),
            "industry": coerce_text(item.get("industry") or item.get("companyIndustry") or ""),
            "company_size": coerce_text(item.get("companySize") or item.get("employeeCount") or ""),
            "source": "apify",
            "source_url": coerce_text(profile),
            "snippet": coerce_text(item.get("summary") or item.get("about") or item.get("headline") or "")[:400],
        }


class ApolloEnricher:
    """
    Enriches leads using Apollo.io People API — finds verified work emails.
    Optional: only active when APOLLO_API_KEY is set.
    """

    BASE_URL = "https://api.apollo.io/api/v1"

    def __init__(self):
        self.api_key = settings.apollo_api_key
        self.available = bool(self.api_key)

    def enrich_lead(
        self,
        name: str,
        company: str,
        domain: str = "",
        linkedin_url: str = "",
    ) -> dict:
        """Find email + enrichment for a person. Returns {} if unavailable."""
        if not self.available:
            return {}

        import httpx
        parts = (name or "").split()
        first = parts[0] if parts else ""
        last = " ".join(parts[1:]) if len(parts) > 1 else ""
        clean_domain = (
            domain.replace("https://", "").replace("http://", "").rstrip("/")
        )
        payload: dict = {
            "first_name": first,
            "last_name": last,
            "organization_name": company,
            "reveal_personal_emails": False,
        }
        if clean_domain:
            payload["domain"] = clean_domain
        if linkedin_url:
            payload["linkedin_url"] = linkedin_url
        try:
            resp = httpx.post(
                f"{self.BASE_URL}/people/match",
                headers={
                    "Content-Type": "application/json",
                    "Cache-Control": "no-cache",
                    "x-api-key": self.api_key,
                },
                json=payload,
                timeout=12,
            )
            if resp.status_code == 200:
                data = resp.json().get("person", {}) or {}
                org = data.get("organization", {}) or {}
                return {
                    "email": data.get("email", "") or "",
                    "email_source": "apollo" if data.get("email") else "",
                    "linkedin_url": data.get("linkedin_url", "") or "",
                    "title": data.get("title", "") or "",
                    "company": (org.get("name", "") or company or ""),
                    "company_website": org.get("website_url", "") or "",
                    "company_size": str(org.get("estimated_num_employees", "") or ""),
                    "industry": org.get("industry", "") or "",
                    "location": ", ".join(
                        x for x in [
                            data.get("city", ""),
                            data.get("state", ""),
                            data.get("country", ""),
                        ] if x
                    ),
                }
        except Exception as e:
            log_agent("ApolloEnricher", f"Enrichment failed for {name}: {e}", "warn")
        return {}


# Singletons
linkedin_scraper = LinkedInScraper()
apollo_enricher = ApolloEnricher()
