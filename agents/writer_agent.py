"""
agents/writer_agent.py
----------------------
Agent 4 — Personalised Message Generation

For each researched lead, generates a channel-specific outreach message:
  - Email: 3-4 paragraph cold email, specific subject line
  - LinkedIn: Connection note + follow-up DM
  - Reddit: Conversational, value-first DM

Runs a quality check pass to eliminate AI-speak and improve naturalness.
Uses the SMART model for maximum writing quality.
"""

from __future__ import annotations
from langchain_core.messages import SystemMessage, HumanMessage
from state.schema import OutreachState, EnrichedLead, OutreachMessage
from utils.helpers import log_agent, safe_json_parse, now_iso, new_id
from utils.llm import fast_llm, smart_llm
from config.settings import settings
from prompts.templates import (
    WRITER_SYSTEM,
    WRITER_EMAIL_USER,
    WRITER_LINKEDIN_USER,
    WRITER_REDDIT_USER,
    QUALITY_CHECK_SYSTEM,
    QUALITY_CHECK_USER,
)


def writer_agent(state: OutreachState) -> dict:
    """
    LangGraph node: generates personalised outreach messages for all researched leads.
    Each message goes through a quality check pass before being added to state.
    """
    log_agent("WriterAgent", "✍️  Generating personalised outreach messages...", "info")

    researched_leads = state.get("researched_leads", [])
    if not researched_leads:
        log_agent("WriterAgent", "No researched leads to write for", "warn")
        return {"messages": []}

    llm = smart_llm(temperature=0.5)  # Slightly creative for natural-sounding prose
    messages_out: list[OutreachMessage] = []

    for lead in researched_leads:
        try:
            msg = _generate_message(lead, llm)
            messages_out.append(msg)
            log_agent(
                "WriterAgent",
                f"✓ Message for {lead.get('name')} via {msg['channel']} "
                f"(personalisation: {msg.get('personalization_score', 0):.0%})",
                "done",
            )
        except Exception as e:
            log_agent("WriterAgent", f"Message generation failed for {lead.get('name')}: {e}", "error")

    log_agent("WriterAgent", f"✓ Generated {len(messages_out)} personalised messages", "done")

    return {
        "messages": messages_out,
        "pending_review": messages_out,  # All go to review queue
        "current_agent": "writer",
        "logs": [log_agent("WriterAgent", f"Generated {len(messages_out)} messages", "done")],
    }


def _generate_message(lead: EnrichedLead, llm) -> OutreachMessage:
    """Generate and quality-check a message for one lead."""
    channel = lead.get("best_channel", "email")
    project_match = lead.get("_project_match", {})

    # Build common context dict
    context = {
        "name": lead.get("name", ""),
        "first_name": lead.get("first_name", lead.get("name", "").split()[0] if lead.get("name") else ""),
        "title": lead.get("title", ""),
        "company": lead.get("company", ""),
        "company_website": lead.get("company_website", ""),
        "industry": lead.get("industry", ""),
        "company_size": lead.get("company_size", ""),
        "location": lead.get("location", ""),
        "company_summary": lead.get("company_summary", ""),
        "recent_news": lead.get("recent_news", "No recent news."),
        "pain_points": "; ".join(lead.get("pain_points", [])[:3]),
        "opportunities": "; ".join(lead.get("opportunities", [])[:2]),
        "tech_stack": ", ".join(lead.get("tech_stack", [])[:4]),
        "project_name": project_match.get("project_name", "NexusIQ"),
        "project_description": project_match.get("project_description", ""),
        "value_proposition": project_match.get("value_proposition", ""),
        "proof_point": project_match.get("proof_point", ""),
        "email": settings.developer_email,
        "portfolio": settings.developer_portfolio,
        "channel": channel,
    }

    # ── Select channel-specific prompt ───────────────────────────────────────
    if channel == "email":
        user_prompt = WRITER_EMAIL_USER.format(**context)
    elif channel == "linkedin":
        user_prompt = WRITER_LINKEDIN_USER.format(**context)
    else:  # reddit / default
        user_prompt = WRITER_REDDIT_USER.format(**context)

    messages = [
        SystemMessage(content=WRITER_SYSTEM),
        HumanMessage(content=user_prompt),
    ]

    response = llm.invoke(messages)
    raw = safe_json_parse(response.content, fallback={})

    subject = raw.get("subject", f"Quick question about {lead.get('company', 'your business')}")
    body = raw.get("body", "")
    tone_score = float(raw.get("tone_score", 0.7))
    personalization_score = float(raw.get("personalization_score", 0.6))

    # ── Quality check pass ───────────────────────────────────────────────────
    if body:
        subject, body = _quality_check(subject, body, channel)

    msg: OutreachMessage = {
        "lead_id": lead.get("id", new_id()),
        "channel": channel,
        "subject": subject,
        "body": body,
        "tone_score": tone_score,
        "personalization_score": personalization_score,
        "approved": None,
        "sent": False,
        "sent_at": None,
        "error": None,
    }

    return msg


def _quality_check(subject: str, body: str, channel: str) -> tuple[str, str]:
    """
    Run a quality check to remove AI-speak and improve naturalness.
    Uses the fast model since it's a refinement pass.
    """
    llm = fast_llm(temperature=0.3)

    prompt = QUALITY_CHECK_USER.format(
        subject=subject,
        body=body,
        channel=channel,
    )

    messages = [
        SystemMessage(content=QUALITY_CHECK_SYSTEM),
        HumanMessage(content=prompt),
    ]

    try:
        response = llm.invoke(messages)
        result = safe_json_parse(response.content, fallback={})

        if result.get("revised_subject"):
            subject = result["revised_subject"]
        if result.get("revised_body"):
            body = result["revised_body"]

    except Exception:
        pass  # If QC fails, return originals — don't lose the message

    return subject, body
