"""
state/schema.py
---------------
Defines the shared LangGraph StateGraph state.
Every agent reads from and writes to this single typed state object,
ensuring type safety and traceable data flow across the pipeline.

v3 — prompt-driven: the pipeline is steered by a natural-language user
prompt that a Planner agent turns into a structured SearchBrief. Leads
pass through a Verifier that labels each one real / partial / unverified.
"""

from __future__ import annotations
from typing import Annotated, Any, Optional
from typing_extensions import TypedDict
import operator


# ─── Search Brief (produced by the Planner from the user prompt) ──────────────

class SearchBrief(TypedDict, total=False):
    """Structured campaign brief distilled from the user's natural-language prompt."""
    goal: str                       # one-line restatement of what the user wants
    target_roles: list[str]         # decision-maker titles to look for
    target_industries: list[str]    # industries / verticals
    locations: list[str]            # geographies (cities / countries / "remote")
    company_size: str               # e.g. "1-50", "50-500"
    keywords: list[str]             # extra signal keywords to search on
    offering: str                   # what the user is selling / offering
    offering_summary: str           # short pitch of the offering
    channels: list[str]             # preferred outreach channels
    exclusions: list[str]           # things/companies to avoid
    search_queries: list[str]       # concrete web/LinkedIn queries to run


# ─── Verification result attached to each lead ───────────────────────────────

class Verification(TypedDict, total=False):
    status: str                     # verified | partial | unverified
    confidence: int                 # 0-100
    domain_live: bool               # company website resolved + reachable
    email_valid: bool               # syntactically valid + domain has MX
    email_source: str               # provided | apollo | pattern-guess | none
    linkedin_valid: bool            # well-formed public profile URL
    checks: list[str]               # human-readable check results


# ─── Sub-models ──────────────────────────────────────────────────────────────

class Lead(TypedDict, total=False):
    """Raw discovered lead before enrichment."""
    id: str
    name: str
    first_name: str
    title: str
    company: str
    company_website: str
    linkedin_url: str
    email: str
    location: str
    industry: str
    company_size: str
    source: str                     # linkedin | tavily | apollo | apify | manual
    source_url: str                 # where this lead was discovered
    snippet: str                    # raw evidence text from discovery


class EnrichedLead(TypedDict, total=False):
    """Lead after research — all fields the Writer Agent needs."""
    # --- Identity ---
    id: str
    name: str
    first_name: str
    title: str
    company: str
    company_website: str
    linkedin_url: str
    email: str
    location: str
    industry: str
    company_size: str
    source: str
    source_url: str
    snippet: str

    # --- Verification ---
    verification: Verification
    verified: bool

    # --- Qualification Score (0-100) ---
    score: int
    score_reasons: list[str]

    # --- Research Intel ---
    company_summary: str
    recent_news: str
    tech_stack: list[str]
    pain_points: list[str]
    opportunities: list[str]
    competitor_context: str

    # --- Outreach Context ---
    best_channel: str               # email | linkedin | reddit
    recommended_service: str
    project_reference: str
    fit_reason: str                 # why this lead fits the offering


class OutreachMessage(TypedDict, total=False):
    """Generated outreach message ready for review/send."""
    lead_id: str
    channel: str                    # email | linkedin | reddit
    subject: Optional[str]
    body: str
    tone_score: float
    personalization_score: float
    approved: Optional[bool]
    human_feedback: Optional[str]
    sent: bool
    sent_at: Optional[str]
    error: Optional[str]


class AgentLog(TypedDict):
    agent: str
    status: str                     # running | done | error
    message: str
    timestamp: str


# ─── Main Graph State ─────────────────────────────────────────────────────────

class OutreachState(TypedDict, total=False):
    """
    Single source of truth for the entire LangGraph pipeline.
    Uses operator.add reducer for list fields so parallel nodes can append.
    """
    # Pipeline control
    run_id: str
    user_prompt: str                # the raw natural-language request
    brief: SearchBrief              # structured plan from the Planner
    max_leads: int

    # Stage 1 — Discovery
    raw_leads: Annotated[list[Lead], operator.add]

    # Stage 2 — Verification
    verified_leads: list[EnrichedLead]

    # Stage 3 — Scoring & Filtering
    scored_leads: Annotated[list[EnrichedLead], operator.add]
    filtered_leads: list[EnrichedLead]

    # Stage 4 — Research
    researched_leads: Annotated[list[EnrichedLead], operator.add]

    # Stage 5 — Message Generation
    messages: Annotated[list[OutreachMessage], operator.add]

    # Stage 6 — Human Review (interrupt point)
    pending_review: list[OutreachMessage]
    approved_messages: list[OutreachMessage]
    rejected_messages: list[OutreachMessage]

    # Stage 7 — Send
    sent_messages: Annotated[list[OutreachMessage], operator.add]
    failed_messages: Annotated[list[OutreachMessage], operator.add]

    # Meta
    logs: Annotated[list[AgentLog], operator.add]
    errors: Annotated[list[str], operator.add]
    current_agent: str
    completed: bool
