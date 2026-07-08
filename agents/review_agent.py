"""
agents/review_agent.py
----------------------
Agent 5 — Human-in-Loop Review (LangGraph Interrupt Point)

Presents each generated message to Shaheer for review.
Supports: approve / edit / skip / auto-approve-all

This is a LangGraph interrupt node — execution pauses here
and resumes when the human provides input.
"""

from __future__ import annotations
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm
from state.schema import OutreachState, OutreachMessage
from utils.helpers import log_agent, now_iso
from config.settings import settings

console = Console()


def review_agent(state: OutreachState) -> dict:
    """
    LangGraph node: interactive human review of generated messages.
    
    If HUMAN_IN_LOOP=false in .env, auto-approves all messages.
    Otherwise, presents each message for review.
    """
    log_agent("ReviewAgent", "👤 Starting human review...", "info")

    pending = state.get("pending_review", [])
    if not pending:
        log_agent("ReviewAgent", "No messages pending review", "warn")
        return {"approved_messages": [], "rejected_messages": []}

    # Auto-approve mode
    if not settings.human_in_loop:
        log_agent("ReviewAgent", "Auto-approving all messages (HUMAN_IN_LOOP=false)", "info")
        approved = [{**m, "approved": True} for m in pending]
        return {
            "approved_messages": approved,
            "rejected_messages": [],
            "current_agent": "review",
        }

    # Interactive review
    approved: list[OutreachMessage] = []
    rejected: list[OutreachMessage] = []

    console.print("\n")
    console.rule("[bold magenta]OUTREACH MESSAGE REVIEW[/bold magenta]")
    console.print(
        f"[dim]Review {len(pending)} messages. Commands: [bold]a[/bold]=approve  "
        f"[bold]s[/bold]=skip  [bold]e[/bold]=edit  [bold]A[/bold]=approve all remaining[/dim]\n"
    )

    auto_approve_rest = False

    # Get researched leads for context lookup
    researched_leads = {l.get("id"): l for l in state.get("researched_leads", [])}

    for i, msg in enumerate(pending, 1):
        if auto_approve_rest:
            approved.append({**msg, "approved": True})
            continue

        # Find associated lead for context
        lead = researched_leads.get(msg.get("lead_id"), {})

        _display_message_card(i, len(pending), msg, lead)

        while True:
            choice = Prompt.ask(
                "[cyan]Action[/cyan]",
                choices=["a", "s", "e", "A"],
                default="a",
            )

            if choice == "a":
                approved.append({**msg, "approved": True})
                console.print("[green]✓ Approved[/green]\n")
                break

            elif choice == "A":
                approved.append({**msg, "approved": True})
                auto_approve_rest = True
                console.print("[green]✓ Approving all remaining...[/green]\n")
                break

            elif choice == "s":
                rejected.append({**msg, "approved": False, "human_feedback": "Skipped"})
                console.print("[yellow]⏭ Skipped[/yellow]\n")
                break

            elif choice == "e":
                msg = _edit_message(msg)
                _display_message_card(i, len(pending), msg, lead)
                # Loop back to let them approve/skip after editing

    console.rule()
    console.print(
        f"[bold]Review complete[/bold]: "
        f"[green]{len(approved)} approved[/green] · "
        f"[yellow]{len(rejected)} skipped[/yellow]"
    )

    return {
        "approved_messages": approved,
        "rejected_messages": rejected,
        "pending_review": [],
        "current_agent": "review",
        "logs": [log_agent("ReviewAgent", f"{len(approved)} approved, {len(rejected)} skipped", "done")],
    }


def _display_message_card(
    index: int,
    total: int,
    msg: OutreachMessage,
    lead: dict,
) -> None:
    """Pretty-print a message for review."""
    channel = msg.get("channel") or "email"
    channel_emoji = {"email": "📧", "linkedin": "💼", "reddit": "🤖"}.get(channel, "📨")

    # Header panel
    header = (
        f"[bold]{index}/{total}[/bold] · {channel_emoji} [cyan]{channel.upper()}[/cyan]\n"
        f"To: [bold]{lead.get('name', 'Unknown')}[/bold] "
        f"({lead.get('title', '')} @ {lead.get('company', '')})\n"
        f"Score: [green]{lead.get('score', 'N/A')}[/green] · "
        f"Personalisation: [blue]{msg.get('personalization_score', 0):.0%}[/blue] · "
        f"Tone: [magenta]{msg.get('tone_score', 0):.0%}[/magenta]"
    )
    console.print(Panel(header, border_style="cyan", padding=(0, 2)))

    # Message content
    if msg.get("subject"):
        console.print(f"[bold]Subject:[/bold] {msg['subject']}\n")
    console.print(Panel(msg.get("body", ""), border_style="white", padding=(1, 2)))


def _edit_message(msg: OutreachMessage) -> OutreachMessage:
    """Allow inline editing of subject and body."""
    console.print("[yellow]Editing message. Press Enter to keep existing value.[/yellow]")

    if msg.get("subject"):
        new_subject = Prompt.ask("Subject", default=msg["subject"])
        msg = {**msg, "subject": new_subject}

    console.print(f"[dim]Current body:[/dim]\n{msg.get('body', '')}\n")
    console.print("[dim]Paste new body (enter END on its own line when done, or press Enter to keep):[/dim]")

    lines = []
    while True:
        line = input()
        if line.strip().upper() == "END":
            break
        if not line and not lines:
            # Empty first line = keep original
            return msg
        lines.append(line)

    if lines:
        msg = {**msg, "body": "\n".join(lines), "human_feedback": "edited"}

    return msg
