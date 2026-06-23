"""
pipeline.py
-----------
The main LangGraph StateGraph pipeline wiring all 6 agents.

Graph flow:
  START
    │
    ▼
  finder_agent        — discovers prospects
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
  review_agent        — human-in-loop approval ← INTERRUPT POINT
    │
    ├── [nothing approved] → END
    │
    ▼
  sender_agent        — dispatches approved messages
    │
    ▼
  END
"""

from __future__ import annotations
from langgraph.graph import StateGraph, START, END
from state.schema import OutreachState
from agents import (
    finder_agent,
    scorer_agent,
    research_agent,
    writer_agent,
    review_agent,
    sender_agent,
)
from utils.helpers import log_agent


# ─── Conditional edges ───────────────────────────────────────────────────────

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

def build_graph() -> StateGraph:
    """
    Construct and compile the LangGraph StateGraph.
    Returns a compiled graph ready for invocation.
    """
    graph = StateGraph(OutreachState)

    # Register nodes (each agent = one node)
    graph.add_node("finder", finder_agent)
    graph.add_node("scorer", scorer_agent)
    graph.add_node("research", research_agent)
    graph.add_node("writer", writer_agent)
    graph.add_node("review", review_agent)
    graph.add_node("sender", sender_agent)

    # Edges (linear flow with conditional branches)
    graph.add_edge(START, "finder")
    graph.add_edge("finder", "scorer")

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
