"""
tools/search.py
---------------
Tavily-powered web search tool for the Research Agent.
Provides structured search with automatic result ranking.
"""

from __future__ import annotations
import asyncio
from typing import Optional
from tavily import TavilyClient
from config.settings import settings
from utils.helpers import log_agent, truncate


class SearchTool:
    """Wrapper around Tavily search API with caching and error handling."""

    def __init__(self):
        self.client = TavilyClient(api_key=settings.tavily_api_key) if settings.tavily_api_key else None
        self._cache: dict[str, str] = {}

    def search(self, query: str, max_results: int = 5) -> str:
        """
        Run a web search and return formatted results string.
        Results are truncated to fit LLM context windows.
        """
        if not self.client:
            return f"[Search unavailable — TAVILY_API_KEY not set] Query was: {query}"

        cache_key = f"{query}:{max_results}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        try:
            response = self.client.search(
                query=query,
                max_results=max_results,
                search_depth="advanced",
                include_answer=True,
            )

            parts = []
            if response.get("answer"):
                parts.append(f"SUMMARY: {truncate(response['answer'], 500)}\n")

            for i, result in enumerate(response.get("results", []), 1):
                title = result.get("title", "")
                url = result.get("url", "")
                content = truncate(result.get("content", ""), 300)
                parts.append(f"[{i}] {title}\n    URL: {url}\n    {content}\n")

            formatted = "\n".join(parts) if parts else "No results found."
            self._cache[cache_key] = formatted
            return formatted

        except Exception as e:
            log_agent("SearchTool", f"Search error for '{query}': {e}", "error")
            return f"Search failed: {str(e)}"

    def search_raw(self, query: str, max_results: int = 5, depth: str = "advanced") -> list[dict]:
        """
        Run a search and return the raw structured results (title, url, content).
        Used by the Finder to extract REAL people/companies from real pages.
        """
        if not self.client:
            return []
        try:
            response = self.client.search(
                query=query,
                max_results=max_results,
                search_depth=depth,
                include_answer=False,
            )
            out = []
            for r in response.get("results", []):
                out.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                    "score": r.get("score", 0),
                })
            return out
        except Exception as e:
            log_agent("SearchTool", f"Raw search error for '{query}': {e}", "warn")
            return []

    def search_company(self, company: str, website: str = "") -> str:
        """Comprehensive company research — runs multiple targeted queries."""
        queries = [
            f"{company} company overview what they do",
            f"{company} recent news 2024 2025",
            f"{company} tech stack technology",
            f"{company} customer problems challenges",
        ]
        if website:
            queries.append(f"site:{website.replace('https://', '').replace('http://', '')} about")

        results = []
        for q in queries[:2]:  # Limit Tavily credits per company on free tier
            result = self.search(q, max_results=3)
            results.append(f"Query: {q}\n{result}")

        return "\n\n---\n\n".join(results)

    def search_person(self, name: str, company: str) -> str:
        """Research a specific person for personalisation."""
        query = f"{name} {company} linkedin founder background"
        return self.search(query, max_results=3)


# Singleton
search_tool = SearchTool()
