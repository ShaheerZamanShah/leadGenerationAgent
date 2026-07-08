"""
agents/writer_agent.py
----------------------
Agent 4 — Personalised Message Generation

For each researched lead, generates a channel-specific outreach message.
Prefers the fast Groq model and falls back to a template message when
rate-limited so the pipeline always completes.
"""

from __future__ import annotations
import time
from langchain_core.messages import SystemMessage, HumanMessage
from state.schema import OutreachState, EnrichedLead, OutreachMessage
from utils.helpers import log_agent, safe_json_parse, new_id
from utils.llm import invoke_smart_or_fast, RateLimitExhausted
from config.settings import settings
from prompts.templates import (
    WRITER_SYSTEM_TEMPLATE,
    WRITER_EMAIL_USER,
    WRITER_LINKEDIN_USER,
    WRITER_REDDIT_USER,
)


def _build_writer_system(brief: dict) -> str:
    """Compose the Writer system prompt from the offering + sender identity."""
    offering = brief.get("offering", settings.default_offering)
    linkedin = settings.developer_linkedin
    return WRITER_SYSTEM_TEMPLATE.format(
        sender_name=settings.developer_name,
        sender_first_name=settings.developer_first_name,
        offering=offering,
        sender_email=settings.developer_email,
        sender_linkedin_line=f" | LinkedIn: {linkedin}" if linkedin else "",
    )


def writer_agent(state: OutreachState) -> dict:
    """
    LangGraph node: generates personalised outreach messages for all researched leads.
    """
    log_agent("WriterAgent", "✍️  Generating personalised outreach messages...", "info")

    researched_leads = state.get("researched_leads", [])
    if not researched_leads:
        log_agent("WriterAgent", "No researched leads to write for", "warn")
        return {"messages": []}

    brief = state.get("brief", {}) or {}
    writer_system = _build_writer_system(brief)
    messages_out: list[OutreachMessage] = []
    use_llm = True

    for i, lead in enumerate(researched_leads):
        try:
            if use_llm:
                msg = _generate_message(lead, writer_system)
            else:
                msg = _template_message(lead)
            messages_out.append(msg)
            log_agent(
                "WriterAgent",
                f"✓ Message for {lead.get('name')} via {msg['channel']} "
                f"(personalisation: {msg.get('personalization_score', 0):.0%})",
                "done",
            )
        except RateLimitExhausted:
            use_llm = False
            log_agent("WriterAgent", "LLM rate-limited — using template messages", "warn")
            msg = _template_message(lead)
            messages_out.append(msg)
            log_agent(
                "WriterAgent",
                f"✓ Template message for {lead.get('name')} via {msg['channel']}",
                "done",
            )
        except Exception as e:
            log_agent("WriterAgent", f"Message generation failed for {lead.get('name')}: {e}", "error")
            messages_out.append(_template_message(lead))

        if i < len(researched_leads) - 1 and use_llm:
            time.sleep(0.6)

    log_agent("WriterAgent", f"✓ Generated {len(messages_out)} personalised messages", "done")

    return {
        "messages": messages_out,
        "pending_review": messages_out,
        "current_agent": "writer",
        "logs": [log_agent("WriterAgent", f"Generated {len(messages_out)} messages", "done")],
    }


def _generate_message(lead: EnrichedLead, writer_system: str) -> OutreachMessage:
    """Generate a message for one lead via LLM (fast model preferred)."""
    channel = lead.get("best_channel") or "email"
    project_match = lead.get("_project_match", {}) or {}

    context = {
        "name": lead.get("name", ""),
        "first_name": lead.get("first_name") or (lead.get("name", "").split()[0] if lead.get("name") else ""),
        "title": lead.get("title", ""),
        "company": lead.get("company", ""),
        "company_website": lead.get("company_website", ""),
        "industry": lead.get("industry", ""),
        "company_size": lead.get("company_size", ""),
        "location": lead.get("location", ""),
        "company_summary": (lead.get("company_summary") or "")[:400],
        "recent_news": (lead.get("recent_news") or "No recent news.")[:200],
        "pain_points": "; ".join(lead.get("pain_points", [])[:3]),
        "opportunities": "; ".join(lead.get("opportunities", [])[:2]),
        "tech_stack": ", ".join(lead.get("tech_stack", [])[:4]),
        "project_name": project_match.get("project_name", "NexusIQ"),
        "project_description": (project_match.get("project_description") or "")[:200],
        "value_proposition": project_match.get("value_proposition", ""),
        "proof_point": project_match.get("proof_point", ""),
        "email": settings.developer_email,
        "portfolio": settings.developer_portfolio,
        "sender_first_name": settings.developer_first_name,
        "channel": channel,
    }

    if channel == "email":
        user_prompt = WRITER_EMAIL_USER.format(**context)
    elif channel == "linkedin":
        user_prompt = WRITER_LINKEDIN_USER.format(**context)
    else:
        user_prompt = WRITER_REDDIT_USER.format(**context)

    messages = [
        SystemMessage(content=writer_system),
        HumanMessage(content=user_prompt),
    ]

    response = invoke_smart_or_fast(
        messages,
        temperature=0.5,
        label="WriterAgent",
        prefer_fast=True,
    )
    raw = safe_json_parse(response.content, fallback={}) or {}

    subject = raw.get("subject") or f"Quick question about {lead.get('company', 'your business')}"
    body = raw.get("body") or ""
    if not body:
        return _template_message(lead)

    return {
        "lead_id": lead.get("id", new_id()),
        "channel": channel,
        "subject": subject,
        "body": body,
        "tone_score": float(raw.get("tone_score", 0.7) or 0.7),
        "personalization_score": float(raw.get("personalization_score", 0.6) or 0.6),
        "approved": None,
        "sent": False,
        "sent_at": None,
        "error": None,
    }


def _template_message(lead: EnrichedLead) -> OutreachMessage:
    """Deterministic personalised message when LLM is unavailable."""
    channel = lead.get("best_channel") or "email"
    first = lead.get("first_name") or (lead.get("name", "").split()[0] if lead.get("name") else "there")
    company = lead.get("company") or "your company"
    pain = (lead.get("pain_points") or ["manual processes slowing the team down"])[0]
    project = (lead.get("_project_match") or {}).get("project_name") or lead.get("project_reference") or "NexusIQ"
    sender = settings.developer_first_name

    if channel == "linkedin":
        subject = f"Quick note for {company}"
        body = (
            f"Hi {first}, I came across {company} and noticed teams in your space often deal with {pain}. "
            f"I've been building {project} to help with exactly that. "
            f"Would you be open to a short chat?\n\nBest,\n{sender}"
        )
    else:
        subject = f"Quick question about {company}"
        body = (
            f"Hi {first},\n\n"
            f"I came across {company} and thought it was worth a quick note. "
            f"A lot of similar teams struggle with {pain}, and I've been helping them with {project}.\n\n"
            f"Would you be open to a 15-minute call this week to see if there's a fit?\n\n"
            f"Best,\n{sender}\n{settings.developer_email}"
        )

    return {
        "lead_id": lead.get("id", new_id()),
        "channel": channel,
        "subject": subject,
        "body": body,
        "tone_score": 0.65,
        "personalization_score": 0.55,
        "approved": None,
        "sent": False,
        "sent_at": None,
        "error": None,
    }
