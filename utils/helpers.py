"""
utils/helpers.py
----------------
Shared utility functions used across all agents.
"""

from __future__ import annotations
import uuid
import json
import re
import threading
from datetime import datetime, timezone
from typing import Any, Callable
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

console = Console()

# SSE / UI listeners — agents always call log_agent(); server registers per-run callbacks.
# Do NOT monkey-patch log_agent: agent modules bind the name at import time, so a patch
# only works for the first campaign and leaves later runs stuck on "Initializing agents".
#
# Listeners are keyed by run_id. A process-wide "active campaign" id routes logs from
# worker threads (ThreadPoolExecutor) that don't inherit thread-locals.
_LogListener = Callable[[dict], None]
_log_listeners: dict[str, _LogListener] = {}
_log_lock = threading.Lock()
_active_campaign_id: str | None = None


def set_active_campaign(run_id: str | None) -> None:
    """Mark which campaign currently owns the live log feed."""
    global _active_campaign_id
    with _log_lock:
        _active_campaign_id = run_id


def get_active_campaign() -> str | None:
    with _log_lock:
        return _active_campaign_id


def add_log_listener(run_id: str, callback: _LogListener) -> _LogListener:
    with _log_lock:
        _log_listeners[run_id] = callback
    return callback


def remove_log_listener(run_id: str) -> None:
    with _log_lock:
        _log_listeners.pop(run_id, None)


def clear_all_log_listeners() -> None:
    with _log_lock:
        _log_listeners.clear()
        global _active_campaign_id
        _active_campaign_id = None


def coerce_text(value: Any, *, join: str = ", ") -> str:
    """Turn Apify/Tavily nested values into a display-safe string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        parts: list[str] = []
        for key in (
            "linkedinText", "default", "full", "name", "city", "country",
            "countryCode", "region", "localizedName", "text", "value",
        ):
            if value.get(key):
                parts.append(coerce_text(value[key], join=join))
        if not parts:
            parts = [coerce_text(v, join=join) for v in value.values() if v]
        return join.join(p for p in parts if p)
    if isinstance(value, list):
        return join.join(coerce_text(v, join=join) for v in value if v)
    return str(value).strip()


def new_id() -> str:
    return str(uuid.uuid4())[:8]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_agent(agent: str, message: str, status: str = "info") -> dict:
    """Create a structured log entry and notify any registered listeners."""
    colours = {"info": "cyan", "done": "green", "error": "red", "warn": "yellow"}
    colour = colours.get(status, "white")
    console.print(f"[{colour}][{agent}][/{colour}] {message}")
    entry = {
        "agent": agent,
        "status": status,
        "message": message,
        "timestamp": now_iso(),
    }
    with _log_lock:
        rid = _active_campaign_id
        cb = _log_listeners.get(rid) if rid else None
    if cb:
        try:
            cb(entry)
        except Exception:
            pass
    return entry


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
