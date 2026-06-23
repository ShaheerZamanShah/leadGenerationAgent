"""
agents/sender_agent.py
----------------------
Agent 6 — Message Dispatch

Sends approved messages via the appropriate channel:
  - Email → Gmail SMTP
  - LinkedIn → Displays instructions (no official API)
  - Reddit → Displays instructions (no official API)

All sends are logged. Supports dry_run mode for testing.
"""

from __future__ import annotations
from rich.console import Console
from rich.panel import Panel
from state.schema import OutreachState, OutreachMessage
from utils.helpers import log_agent, now_iso
from tools.email_sender import gmail_sender
from config.settings import settings

console = Console()


def sender_agent(state: OutreachState) -> dict:
    """
    LangGraph node: dispatches approved messages.
    
    Email → sends via Gmail SMTP
    LinkedIn/Reddit → prints formatted message for manual copy-paste
                      (official APIs require business accounts)
    """
    log_agent("SenderAgent", "📤 Dispatching approved messages...", "info")

    approved = state.get("approved_messages", [])
    if not approved:
        log_agent("SenderAgent", "No approved messages to send", "warn")
        return {"sent_messages": [], "failed_messages": []}

    # Lookup lead details for email address
    researched_leads = {l.get("id"): l for l in state.get("researched_leads", [])}

    sent: list[OutreachMessage] = []
    failed: list[OutreachMessage] = []

    for msg in approved:
        lead = researched_leads.get(msg.get("lead_id"), {})
        channel = msg.get("channel", "email")

        if channel == "email":
            result = _send_email(msg, lead)
        else:
            result = _display_for_manual_send(msg, lead, channel)

        if result["success"]:
            sent.append({**msg, "sent": True, "sent_at": now_iso()})
        else:
            failed.append({**msg, "sent": False, "error": result.get("error", "Unknown error")})

    console.rule()
    console.print(
        f"[bold]Send complete:[/bold] "
        f"[green]{len(sent)} sent[/green] · "
        f"[red]{len(failed)} failed[/red]"
    )

    return {
        "sent_messages": sent,
        "failed_messages": failed,
        "completed": True,
        "current_agent": "sender",
        "logs": [log_agent("SenderAgent", f"{len(sent)} sent, {len(failed)} failed", "done")],
    }


def _send_email(msg: OutreachMessage, lead: dict) -> dict:
    """Send email via Gmail SMTP."""
    to_email = lead.get("email", "")

    if not to_email:
        log_agent("SenderAgent", f"No email for {lead.get('name')} — skipping", "warn")
        return {"success": False, "error": "No email address found"}

    success, status = gmail_sender.send(
        to=to_email,
        subject=msg.get("subject", ""),
        body=msg.get("body", ""),
        dry_run=not gmail_sender.available,  # Dry run if Gmail not configured
    )

    if success:
        log_agent("SenderAgent", f"✓ Email sent to {to_email}", "done")
    else:
        log_agent("SenderAgent", f"✗ Email failed to {to_email}: {status}", "error")

    return {"success": success, "error": status if not success else None}


def _display_for_manual_send(
    msg: OutreachMessage,
    lead: dict,
    channel: str,
) -> dict:
    """
    Display message for manual copy-paste on LinkedIn/Reddit.
    (These platforms don't have accessible messaging APIs)
    """
    channel_emoji = {"linkedin": "💼", "reddit": "🤖"}.get(channel, "📨")
    profile_url = lead.get("linkedin_url", "") if channel == "linkedin" else ""

    console.print(f"\n{channel_emoji} [bold cyan]{channel.upper()} MESSAGE[/bold cyan]")
    console.print(f"[dim]To:[/dim] {lead.get('name', '')} @ {lead.get('company', '')}")
    if profile_url:
        console.print(f"[dim]Profile:[/dim] [link={profile_url}]{profile_url}[/link]")

    if msg.get("subject"):
        console.print(f"\n[bold]Message 1 (Connection Note):[/bold]")
        console.print(Panel(msg["subject"], border_style="blue"))
        console.print(f"\n[bold]Message 2 (Follow-up DM):[/bold]")
        console.print(Panel(msg.get("body", ""), border_style="blue"))
    else:
        console.print(Panel(msg.get("body", ""), border_style="blue"))

    console.print("[dim]→ Copy the above and send manually via the platform[/dim]\n")

    # Mark as "sent" (we've done our part — it's ready for copy-paste)
    return {"success": True}
