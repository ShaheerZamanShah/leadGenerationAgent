"""
utils/reporter.py
-----------------
Saves pipeline results to disk:
  - leads_report.csv  — qualified lead list with scores
  - messages_report.csv — all generated messages
  - full_run_<run_id>.json — complete state dump

Run summary is also printed to console.
"""

from __future__ import annotations
import json
import csv
from pathlib import Path
from datetime import datetime
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from state.schema import OutreachState
from config.settings import settings
from utils.helpers import sanitize_filename

console = Console()


def save_results(state: OutreachState) -> Path:
    """
    Save pipeline results to disk and print summary.
    Returns the output directory path.
    """
    run_id = state.get("run_id", "unknown")
    output_dir = settings.output_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Save leads CSV ────────────────────────────────────────────────────────
    leads_path = output_dir / "leads_report.csv"
    _save_leads_csv(state, leads_path)

    # ── Save messages CSV ─────────────────────────────────────────────────────
    messages_path = output_dir / "messages_report.csv"
    _save_messages_csv(state, messages_path)

    # ── Save full JSON dump ───────────────────────────────────────────────────
    json_path = output_dir / f"full_run_{run_id}.json"
    _save_json_dump(state, json_path)

    # ── Print summary ─────────────────────────────────────────────────────────
    _print_summary(state, output_dir)

    return output_dir


def _save_leads_csv(state: OutreachState, path: Path) -> None:
    researched = state.get("researched_leads", [])
    if not researched:
        return

    fieldnames = [
        "name", "title", "company", "industry", "company_size",
        "location", "email", "linkedin_url", "company_website", "score",
        "verification_status", "verification_confidence",
        "recommended_service", "best_channel", "source", "source_url", "pain_points",
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for lead in researched:
            verification = lead.get("verification", {}) or {}
            row = {
                **lead,
                "pain_points": "; ".join(lead.get("pain_points", [])[:3]),
                "verification_status": verification.get("status", ""),
                "verification_confidence": verification.get("confidence", ""),
            }
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def _save_messages_csv(state: OutreachState, path: Path) -> None:
    all_messages = (
        state.get("sent_messages", [])
        + state.get("approved_messages", [])
        + state.get("failed_messages", [])
    )
    if not all_messages:
        return

    # Lookup lead names
    lead_map = {l.get("id"): l for l in state.get("researched_leads", [])}

    fieldnames = ["lead_name", "company", "channel", "subject", "body", "score",
                  "personalization_score", "tone_score", "approved", "sent", "error"]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for msg in all_messages:
            lead = lead_map.get(msg.get("lead_id"), {})
            row = {
                **msg,
                "lead_name": lead.get("name", ""),
                "company": lead.get("company", ""),
                "score": lead.get("score", ""),
            }
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def _save_json_dump(state: OutreachState, path: Path) -> None:
    # Remove non-serializable internal fields
    clean_state = {
        k: v for k, v in state.items()
        if not k.startswith("_")
    }
    # Remove _project_match from leads
    for lead in clean_state.get("researched_leads", []):
        lead.pop("_project_match", None)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(clean_state, f, indent=2, default=str)


def _print_summary(state: OutreachState, output_dir: Path) -> None:
    raw = len(state.get("raw_leads", []))
    verified_leads = state.get("verified_leads", [])
    verified = sum(
        1 for l in verified_leads
        if l.get("verification", {}).get("status") in ("verified", "partial")
    )
    qualified = len(state.get("filtered_leads", []))
    researched = len(state.get("researched_leads", []))
    generated = len(state.get("messages", []))
    approved = len(state.get("approved_messages", []))
    sent = len(state.get("sent_messages", []))
    failed = len(state.get("failed_messages", []))

    table = Table(title="🚀 Pipeline Results", border_style="magenta")
    table.add_column("Stage", style="cyan")
    table.add_column("Count", style="bold white", justify="right")

    table.add_row("Prospects discovered", str(raw))
    table.add_row("Verified real leads", str(verified))
    table.add_row("Qualified leads (≥ score threshold)", str(qualified))
    table.add_row("Leads researched", str(researched))
    table.add_row("Messages generated", str(generated))
    table.add_row("Messages approved", str(approved))
    table.add_row("Messages sent ✓", str(sent))
    if failed:
        table.add_row("[red]Failed sends[/red]", f"[red]{failed}[/red]")

    console.print("\n")
    console.print(table)
    console.print(
        Panel(
            f"[bold]Results saved to:[/bold] [cyan]{output_dir}[/cyan]\n"
            f"  • leads_report.csv\n"
            f"  • messages_report.csv\n"
            f"  • full_run_*.json",
            border_style="green",
        )
    )
