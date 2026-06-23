"""
utils/helpers.py
----------------
Shared utility functions used across all agents.
"""

from __future__ import annotations
import uuid
import json
import re
from datetime import datetime, timezone
from typing import Any
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()


def new_id() -> str:
    return str(uuid.uuid4())[:8]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_agent(agent: str, message: str, status: str = "info") -> dict:
    """Create a structured log entry."""
    colours = {"info": "cyan", "done": "green", "error": "red", "warn": "yellow"}
    colour = colours.get(status, "white")
    console.print(f"[{colour}][{agent}][/{colour}] {message}")
    return {
        "agent": agent,
        "status": status,
        "message": message,
        "timestamp": now_iso(),
    }


def safe_json_parse(text: str, fallback: Any = None) -> Any:
    """Parse JSON from LLM output, stripping markdown fences."""
    # Strip ```json ... ``` fences
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "").strip()
    # Find first { or [ to handle preamble text
    for i, ch in enumerate(cleaned):
        if ch in "{[":
            cleaned = cleaned[i:]
            break
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return fallback


def truncate(text: str, max_chars: int = 400) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "…"


def print_banner(title: str, subtitle: str = "") -> None:
    text = Text(title, style="bold magenta")
    if subtitle:
        text.append(f"\n{subtitle}", style="dim white")
    console.print(Panel(text, border_style="magenta", padding=(1, 4)))


def print_lead_card(lead: dict) -> None:
    lines = [
        f"[bold]{lead.get('name', 'Unknown')}[/bold] — {lead.get('title', '')}",
        f"[cyan]{lead.get('company', '')}[/cyan] · {lead.get('industry', '')}",
        f"Score: [green]{lead.get('score', 'N/A')}[/green] | Channel: {lead.get('best_channel', 'email')}",
        f"Pain points: {', '.join(lead.get('pain_points', [])[:2])}",
    ]
    console.print(Panel("\n".join(lines), border_style="blue", padding=(0, 2)))


def sanitize_filename(s: str) -> str:
    return re.sub(r"[^\w\-]", "_", s)[:40]
