"""
agents/planner_agent.py
-----------------------
Agent 0 — Campaign Planner

Turns the user's natural-language prompt into a structured SearchBrief that
steers the whole pipeline (roles, industries, locations, keywords, the
offering, and concrete search queries).

Outputs: brief written to state.
"""

from __future__ import annotations
from langchain_core.messages import SystemMessage, HumanMessage
from state.schema import OutreachState, SearchBrief
from utils.helpers import log_agent, safe_json_parse
from utils.llm import invoke_smart_or_fast
from config.settings import settings
from prompts.templates import PLANNER_SYSTEM, PLANNER_USER


def _fallback_brief(prompt: str) -> SearchBrief:
    """A safe default brief if the LLM fails, using global config defaults."""
    return {
        "goal": prompt.strip()[:200] or "Find qualified B2B decision-makers",
        "target_roles": settings.target_roles[:8],
        "target_industries": settings.target_industries[:10],
        "locations": ["Global"],
        "company_size": "1-500",
        "keywords": [],
        "offering": settings.default_offering,
        "offering_summary": settings.default_offering_summary,
        "channels": ["email", "linkedin"],
        "exclusions": [],
        "search_queries": [
            'site:linkedin.com/in "Founder" startup',
            'site:linkedin.com/in "CEO" SaaS',
            '"head of operations" startup email contact',
        ],
    }


def planner_agent(state: OutreachState) -> dict:
    """LangGraph node: parse the user prompt into a structured brief."""
    prompt = (state.get("user_prompt") or "").strip()
    log_agent("PlannerAgent", "🧭 Planning campaign from your prompt...", "info")

    if not prompt:
        log_agent("PlannerAgent", "No prompt provided — using default campaign brief", "warn")
        brief = _fallback_brief("")
        return {"brief": brief, "current_agent": "planner",
                "logs": [log_agent("PlannerAgent", "Using default brief", "done")]}

    try:
        response = invoke_smart_or_fast(
            [
                SystemMessage(content=PLANNER_SYSTEM),
                HumanMessage(content=PLANNER_USER.format(
                    user_prompt=prompt,
                    default_offering=settings.default_offering,
                )),
            ],
            temperature=0.3,
            label="PlannerAgent",
            prefer_fast=True,
        )
        data = safe_json_parse(response.content, fallback={})
    except Exception as e:
        log_agent("PlannerAgent", f"Planning failed ({e}) — using fallback", "warn")
        data = {}

    if not isinstance(data, dict) or not data.get("goal"):
        brief = _fallback_brief(prompt)
    else:
        fb = _fallback_brief(prompt)
        brief = {
            "goal": data.get("goal", fb["goal"]),
            "target_roles": data.get("target_roles") or fb["target_roles"],
            "target_industries": data.get("target_industries") or fb["target_industries"],
            "locations": data.get("locations") or fb["locations"],
            "company_size": data.get("company_size") or fb["company_size"],
            "keywords": data.get("keywords") or [],
            "offering": data.get("offering") or settings.default_offering,
            "offering_summary": data.get("offering_summary") or settings.default_offering_summary,
            "channels": data.get("channels") or ["email", "linkedin"],
            "exclusions": data.get("exclusions") or [],
            "search_queries": data.get("search_queries") or fb["search_queries"],
        }

    log_agent(
        "PlannerAgent",
        f"✓ Brief ready — targeting {', '.join(brief['target_roles'][:3])} "
        f"in {', '.join(brief['target_industries'][:3])}",
        "done",
    )
    return {
        "brief": brief,
        "current_agent": "planner",
        "logs": [log_agent("PlannerAgent", f"Goal: {brief['goal']}", "done")],
    }
