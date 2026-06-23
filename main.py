"""
main.py
-------
CLI entrypoint for the Outreach Agent pipeline.

Usage:
  python main.py                    # Full pipeline run
  python main.py --dry-run          # Run without sending emails
  python main.py --leads 10         # Override max leads
  python main.py --no-review        # Auto-approve (skip human review)
  python main.py --from-csv leads.csv   # Load leads from CSV instead of scraping
"""

from __future__ import annotations
import argparse
import csv
import sys
import uuid
from pathlib import Path

from rich.console import Console

from pipeline import outreach_graph
from state.schema import OutreachState
from config.settings import settings
from utils.helpers import print_banner, log_agent, new_id
from utils.reporter import save_results

console = Console()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Outreach Agent — AI-powered B2B lead generation & personalised messaging"
    )
    parser.add_argument("--dry-run", action="store_true", help="Don't send emails, just display")
    parser.add_argument("--leads", type=int, default=None, help="Max leads to process")
    parser.add_argument("--no-review", action="store_true", help="Auto-approve all messages")
    parser.add_argument("--from-csv", type=str, default=None, help="Load leads from CSV file")
    parser.add_argument("--industries", nargs="*", default=None, help="Target industries override")
    parser.add_argument("--roles", nargs="*", default=None, help="Target roles override")
    return parser.parse_args()


def load_leads_from_csv(path: str) -> list[dict]:
    """Load pre-existing leads from a CSV file."""
    leads = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lead = {
                "id": row.get("id", new_id()),
                "name": row.get("name", ""),
                "first_name": row.get("first_name", row.get("name", "").split()[0] if row.get("name") else ""),
                "title": row.get("title", ""),
                "company": row.get("company", ""),
                "company_website": row.get("company_website", row.get("website", "")),
                "linkedin_url": row.get("linkedin_url", ""),
                "email": row.get("email", ""),
                "location": row.get("location", ""),
                "industry": row.get("industry", ""),
                "company_size": row.get("company_size", ""),
                "source": row.get("source", "manual"),
            }
            if lead["name"] or lead["company"]:
                leads.append(lead)
    return leads


def main() -> None:
    args = parse_args()

    print_banner(
        "🤖 Outreach Agent",
        "AI/ML & Agentic AI Developer — Automated B2B Lead Generation & Outreach",
    )

    # ── Validate configuration ────────────────────────────────────────────────
    missing = settings.validate()
    if missing:
        console.print(f"[red]⚠ Missing required API keys: {', '.join(missing)}[/red]")
        console.print("[dim]Copy .env.example to .env and fill in your keys.[/dim]")
        if "GROQ_API_KEY" in missing:
            sys.exit(1)  # Can't run without LLM

    # ── Apply CLI overrides ───────────────────────────────────────────────────
    if args.no_review:
        settings.human_in_loop = False
    if args.leads:
        settings.max_leads_per_run = args.leads

    run_id = str(uuid.uuid4())[:8]

    # ── Build initial state ───────────────────────────────────────────────────
    initial_state: OutreachState = {
        "run_id": run_id,
        "target_industries": args.industries or settings.target_industries,
        "target_roles": args.roles or settings.target_roles,
        "max_leads": args.leads or settings.max_leads_per_run,
        "raw_leads": [],
        "scored_leads": [],
        "filtered_leads": [],
        "researched_leads": [],
        "messages": [],
        "pending_review": [],
        "approved_messages": [],
        "rejected_messages": [],
        "sent_messages": [],
        "failed_messages": [],
        "logs": [],
        "errors": [],
        "current_agent": "init",
        "completed": False,
    }

    # ── Pre-load from CSV if provided ─────────────────────────────────────────
    if args.from_csv:
        csv_path = Path(args.from_csv)
        if not csv_path.exists():
            console.print(f"[red]CSV file not found: {csv_path}[/red]")
            sys.exit(1)

        leads = load_leads_from_csv(str(csv_path))
        console.print(f"[cyan]Loaded {len(leads)} leads from {csv_path}[/cyan]")

        # Skip finder agent, inject directly into raw_leads
        # The graph will still score/filter/research/write/review/send
        initial_state["raw_leads"] = leads

    # ── Run the pipeline ──────────────────────────────────────────────────────
    console.print(f"\n[dim]Run ID: {run_id}[/dim]")
    console.print(f"[dim]Max leads: {initial_state['max_leads']} | Human review: {settings.human_in_loop}[/dim]\n")

    try:
        # If CSV was provided, we need a version of the graph that starts at scorer
        # Otherwise run full graph from START
        final_state = outreach_graph.invoke(
            initial_state,
            config={"configurable": {"thread_id": run_id}},
        )

        # ── Save results ──────────────────────────────────────────────────────
        output_dir = save_results(final_state)

    except KeyboardInterrupt:
        console.print("\n[yellow]Pipeline interrupted by user[/yellow]")
        sys.exit(0)
    except Exception as e:
        console.print(f"\n[red]Pipeline error: {e}[/red]")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
