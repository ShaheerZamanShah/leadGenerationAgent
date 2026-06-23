"""
state/schema.py
---------------
Defines the shared LangGraph StateGraph state.
Every agent reads from and writes to this single typed state object,
ensuring type safety and traceable data flow across the pipeline.
"""

from __future__ import annotations
from typing import Annotated, Any, Optional
from typing_extensions import TypedDict
import operator


# ─── Sub-models ──────────────────────────────────────────────────────────────

class Lead(TypedDict, total=False):
    """Raw discovered lead before enrichment."""
    id: str
    name: str
    title: str
    company: str
    company_website: str
    linkedin_url: str
    email: str
    location: str
    industry: str
    company_size: str
    source: str  # linkedin | reddit | apollo | manual


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

    # --- Qualification Score (0-100) ---
    score: int
    score_reasons: list[str]

    # --- Research Intel ---
    company_summary: str          # 2-3 sentence overview
    recent_news: str              # latest news / product launches
    tech_stack: list[str]         # detected technologies
    pain_points: list[str]        # inferred AI/automation gaps
    opportunities: list[str]      # specific services Shaheer can offer
    competitor_context: str       # competitive landscape

    # --- Outreach Context ---
    best_channel: str             # email | linkedin | reddit
    recommended_service: str      # which of Shaheer's services fits best
    project_reference: str        # which of Shaheer's projects to cite


class OutreachMessage(TypedDict, total=False):
    """Generated outreach message ready for review/send."""
    lead_id: str
    channel: str                  # email | linkedin | reddit
    subject: Optional[str]        # email only
    body: str
    tone_score: float             # 0-1, how human-like
    personalization_score: float  # 0-1
    approved: Optional[bool]      # human review
    human_feedback: Optional[str]
    sent: bool
    sent_at: Optional[str]
    error: Optional[str]


class AgentLog(TypedDict):
    agent: str
    status: str   # running | done | error
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
    target_industries: list[str]
    target_roles: list[str]
    max_leads: int

    # Stage 1 — Discovery
    raw_leads: Annotated[list[Lead], operator.add]

    # Stage 2 — Scoring & Filtering
    scored_leads: Annotated[list[EnrichedLead], operator.add]
    filtered_leads: list[EnrichedLead]   # above threshold only

    # Stage 3 — Research
    researched_leads: Annotated[list[EnrichedLead], operator.add]

    # Stage 4 — Message Generation
    messages: Annotated[list[OutreachMessage], operator.add]

    # Stage 5 — Human Review (interrupt point)
    pending_review: list[OutreachMessage]
    approved_messages: list[OutreachMessage]
    rejected_messages: list[OutreachMessage]

    # Stage 6 — Send
    sent_messages: Annotated[list[OutreachMessage], operator.add]
    failed_messages: Annotated[list[OutreachMessage], operator.add]

    # Meta
    logs: Annotated[list[AgentLog], operator.add]
    errors: Annotated[list[str], operator.add]
    current_agent: str
    completed: bool
