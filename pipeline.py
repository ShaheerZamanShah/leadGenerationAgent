"""
pipeline.py
-----------
The main LangGraph StateGraph pipeline wiring all agents.

Graph flow:
  START
    │
    ▼
  planner_agent       — prompt → structured campaign brief
    │
    ├── [pre-loaded leads / --from-csv] → verifier
    │
    ▼
  finder_agent        — discovers REAL prospects (LinkedIn + web)
    │
    ├── [no leads] → END
    │
    ▼
  verifier_agent      — verifies domains, emails, LinkedIn (real check)
    │
    ▼
  scorer_agent        — qualifies & filters leads (0-100 score)
    │
    ├── [no qualified leads] → END
    │
    ▼
  research_agent      — deep-researches each lead (parallel)
    │
    ▼
  writer_agent        — generates personalised messages
    │
    ▼
  review_agent        — human-in-loop approval (auto in web mode)
    │
    ├── [nothing approved] → END
    │
    ▼
  sender_agent        — dispatches / prepares approved messages
    │
    ▼
  END
"""

from __future__ import annotations
from langgraph.graph import StateGraph, START, END
from state.schema import OutreachState
from agents import (
    planner_agent,
    finder_agent,
    verifier_agent,
    scorer_agent,
    research_agent,
    writer_agent,
    review_agent,
    sender_agent,
)
from utils.helpers import log_agent


# ─── Conditional edges ───────────────────────────────────────────────────────

def after_planner(state: OutreachState) -> str:
    """Route: skip finder when leads were pre-loaded (e.g. --from-csv)."""
    if state.get("skip_discovery") or state.get("raw_leads"):
        log_agent("Pipeline", "Pre-loaded leads detected — skipping discovery", "info")
        return "verifier"
    return "finder"


def after_finder(state: OutreachState) -> str:
    """Route: if no prospects discovered, end early."""
    if not state.get("raw_leads"):
        log_agent("Pipeline", "No prospects discovered — ending pipeline", "warn")
        return "end"
    return "verifier"


def after_verifier(state: OutreachState) -> str:
    """Route: if verifier kept no leads, end early (respect strict filtering)."""
    if not state.get("verified_leads"):
        log_agent("Pipeline", "No verified leads after verification — ending pipeline", "warn")
        return "end"
    return "scorer"


def after_scorer(state: OutreachState) -> str:
    """Route: if no qualified leads, end early."""
    if not state.get("filtered_leads"):
        log_agent("Pipeline", "No qualified leads after scoring — ending pipeline", "warn")
        return "end"
    return "research"


def after_review(state: OutreachState) -> str:
    """Route: if nothing approved, end early."""
    if not state.get("approved_messages"):
        log_agent("Pipeline", "No approved messages — skipping send", "warn")
        return "end"
    return "sender"


# ─── Graph construction ───────────────────────────────────────────────────────

def build_graph():
    """Construct and compile the LangGraph StateGraph."""
    graph = StateGraph(OutreachState)

    graph.add_node("planner", planner_agent)
    graph.add_node("finder", finder_agent)
    graph.add_node("verifier", verifier_agent)
    graph.add_node("scorer", scorer_agent)
    graph.add_node("research", research_agent)
    graph.add_node("writer", writer_agent)
    graph.add_node("review", review_agent)
    graph.add_node("sender", sender_agent)

    graph.add_edge(START, "planner")

    graph.add_conditional_edges(
        "planner",
        after_planner,
        {"finder": "finder", "verifier": "verifier"},
    )

    graph.add_conditional_edges(
        "finder",
        after_finder,
        {"verifier": "verifier", "end": END},
    )

    graph.add_conditional_edges(
        "verifier",
        after_verifier,
        {"scorer": "scorer", "end": END},
    )

    graph.add_conditional_edges(
        "scorer",
        after_scorer,
        {"research": "research", "end": END},
    )

    graph.add_edge("research", "writer")
    graph.add_edge("writer", "review")

    graph.add_conditional_edges(
        "review",
        after_review,
        {"sender": "sender", "end": END},
    )

    graph.add_edge("sender", END)

    return graph.compile()


# Singleton compiled graph
outreach_graph = build_graph()
