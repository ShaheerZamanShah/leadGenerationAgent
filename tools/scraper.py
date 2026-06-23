"""
tools/scraper.py
----------------
Apify-powered scraping tools for LinkedIn, Reddit, and Apollo.
Falls back gracefully when API keys are not set.
"""

from __future__ import annotations
from typing import Optional
from utils.helpers import log_agent, new_id

try:
    from apify_client import ApifyClient
    APIFY_AVAILABLE = True
except ImportError:
    APIFY_AVAILABLE = False

from config.settings import settings


class LinkedInScraper:
    """
    Scrapes LinkedIn for people matching target criteria.
    Uses Apify's LinkedIn People Profile Scraper actor.
    Actor: https://apify.com/anchor/linkedin-profile-scraper
    """

    ACTOR_ID = "2SyF0bVxmgGr8IVCZ"  # LinkedIn People Search actor

    def __init__(self):
        self.client = (
            ApifyClient(settings.apify_api_key)
            if APIFY_AVAILABLE and settings.apify_api_key
            else None
        )

    def search_leads(
        self,
        roles: list[str],
        industries: list[str],
        max_results: int = 20,
    ) -> list[dict]:
        """
        Search LinkedIn for decision-makers in target industries.
        Returns list of raw lead dicts.
        """
        if not self.client:
            log_agent("LinkedInScraper", "Apify not available — returning empty list", "warn")
            return []

        leads = []
        for role in roles[:3]:  # Limit roles to control costs
            for industry in industries[:3]:
                try:
                    run_input = {
                        "searchUrl": (
                            f"https://www.linkedin.com/search/results/people/"
                            f"?keywords={role.replace(' ', '%20')}"
                            f"&industry={industry.replace(' ', '%20')}"
                        ),
                        "maxResults": max_results // (3 * 3),
                    }
                    run = self.client.actor(self.ACTOR_ID).call(run_input=run_input)
                    items = list(self.client.dataset(run["defaultDatasetId"]).iterate_items())

                    for item in items:
                        lead = self._map_item(item, role, industry)
                        if lead:
                            leads.append(lead)

                except Exception as e:
                    log_agent("LinkedInScraper", f"Scrape error: {e}", "warn")

        return leads[:max_results]

    def _map_item(self, item: dict, role: str, industry: str) -> dict | None:
        """Map Apify result to our Lead schema."""
        name = item.get("fullName") or item.get("name", "")
        if not name:
            return None
        return {
            "id": new_id(),
            "name": name,
            "first_name": name.split()[0] if name else "",
            "title": item.get("headline", role),
            "company": item.get("companyName", ""),
            "company_website": item.get("companyUrl", ""),
            "linkedin_url": item.get("profileUrl", ""),
            "email": item.get("email", ""),
            "location": item.get("location", ""),
            "industry": industry,
            "company_size": item.get("companySize", "unknown"),
            "source": "linkedin",
        }


class ApolloEnricher:
    """
    Enriches leads using Apollo.io People API.
    Finds verified email addresses for cold outreach.
    """

    BASE_URL = "https://api.apollo.io/v1"

    def __init__(self):
        self.api_key = settings.apollo_api_key
        self.available = bool(self.api_key)

    def enrich_lead(self, name: str, company: str, domain: str = "") -> dict:
        """
        Try to find email and enrichment data for a person.
        Returns partial dict with email if found.
        """
        if not self.available:
            return {}

        import httpx
        try:
            resp = httpx.post(
                f"{self.BASE_URL}/people/match",
                headers={"Content-Type": "application/json", "Cache-Control": "no-cache"},
                json={
                    "api_key": self.api_key,
                    "first_name": name.split()[0],
                    "last_name": " ".join(name.split()[1:]) if len(name.split()) > 1 else "",
                    "organization_name": company,
                    "domain": domain.replace("https://", "").replace("http://", "").rstrip("/"),
                },
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json().get("person", {})
                return {
                    "email": data.get("email", ""),
                    "linkedin_url": data.get("linkedin_url", ""),
                    "title": data.get("title", ""),
                    "company_size": str(data.get("organization", {}).get("estimated_num_employees", "")),
                }
        except Exception as e:
            log_agent("ApolloEnricher", f"Enrichment failed for {name}: {e}", "warn")
        return {}


# Singletons
linkedin_scraper = LinkedInScraper()
apollo_enricher = ApolloEnricher()
