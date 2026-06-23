"""
server.py
---------
FastAPI backend for the Outreach Agent web app.

Endpoints:
  GET  /                      → serves frontend/index.html
  GET  /frontend/<file>       → serves static frontend assets
  POST /api/start-campaign    → starts the agent pipeline
  GET  /api/stream/{run_id}   → SSE stream of live logs + results
  GET  /api/results/{run_id}  → full JSON results
  GET  /api/status/{run_id}   → pipeline status
  GET  /api/cv-info           → Shaheer's CV summary
"""

from __future__ import annotations
import json
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ── Project imports ───────────────────────────────────────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import settings
from utils.cv_parser import get_cv_summary

app = FastAPI(title="Outreach Agent API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── State store ───────────────────────────────────────────────────────────────
_runs: dict[str, dict] = {}          # run_id → { status, state, queue }


# ── Request / Response models ─────────────────────────────────────────────────
class CampaignRequest(BaseModel):
    max_leads: int = 20
    industries: list[str] = []
    roles: list[str] = []
    no_review: bool = True
    dry_run: bool = True


# ── Static files ──────────────────────────────────────────────────────────────
FRONTEND_DIR = Path(__file__).parent / "frontend"
FRONTEND_DIR.mkdir(exist_ok=True)

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

@app.get("/frontend/{filename:path}")
async def serve_static(filename: str):
    file_path = FRONTEND_DIR / filename
    if file_path.exists():
        return FileResponse(str(file_path))
    raise HTTPException(status_code=404, detail="File not found")


# ── CV Info endpoint ──────────────────────────────────────────────────────────
@app.get("/api/cv-info")
async def get_cv_info():
    cv = get_cv_summary()
    return {
        "skills": cv["skills"],
        "projects": [
            {"name": p["name"], "description": p["description"], "proof": p["proof"]}
            for p in cv["projects"]
        ],
        "experience": cv["experience"],
        "education": cv["education"],
    }


# ── Campaign endpoints ────────────────────────────────────────────────────────
@app.post("/api/start-campaign")
async def start_campaign(req: CampaignRequest):
    run_id = str(uuid.uuid4())[:8]
    event_queue: queue.Queue = queue.Queue()

    _runs[run_id] = {
        "status": "starting",
        "queue": event_queue,
        "state": None,
        "error": None,
    }

    # Modify settings for this run
    if req.no_review:
        settings.human_in_loop = False
    if req.max_leads:
        settings.max_leads_per_run = req.max_leads
    if req.industries:
        settings.target_industries = req.industries
    if req.roles:
        settings.target_roles = req.roles

    # Run pipeline in background thread
    thread = threading.Thread(
        target=_run_pipeline,
        args=(run_id, req, event_queue),
        daemon=True,
    )
    thread.start()

    return {"run_id": run_id, "status": "started"}


@app.get("/api/stream/{run_id}")
async def stream_events(run_id: str):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Run not found")

    def event_generator():
        run = _runs[run_id]
        q: queue.Queue = run["queue"]
        while True:
            try:
                event = q.get(timeout=30)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") == "done" or event.get("type") == "error":
                    break
            except queue.Empty:
                # Send keepalive
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/results/{run_id}")
async def get_results(run_id: str):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Run not found")
    run = _runs[run_id]
    if run["status"] != "done":
        raise HTTPException(status_code=202, detail="Pipeline still running")
    return JSONResponse(content=run.get("state") or {})


@app.get("/api/status/{run_id}")
async def get_status(run_id: str):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Run not found")
    run = _runs[run_id]
    return {
        "run_id": run_id,
        "status": run["status"],
        "error": run.get("error"),
    }


# ── Pipeline runner (background thread) ───────────────────────────────────────
def _run_pipeline(run_id: str, req: CampaignRequest, q: queue.Queue):
    """Run the LangGraph pipeline and push events to the SSE queue."""
    try:
        _runs[run_id]["status"] = "running"
        q.put({"type": "status", "message": "Pipeline starting...", "run_id": run_id})

        # Monkey-patch log_agent to also emit SSE events
        import utils.helpers as helpers_mod
        original_log = helpers_mod.log_agent

        def patched_log(agent: str, message: str, status: str = "info") -> dict:
            log_entry = original_log(agent, message, status)
            q.put({
                "type": "log",
                "agent": agent,
                "message": message,
                "status": status,
                "timestamp": log_entry["timestamp"],
            })
            return log_entry

        helpers_mod.log_agent = patched_log

        # Build initial state
        from state.schema import OutreachState
        initial_state: OutreachState = {
            "run_id": run_id,
            "target_industries": req.industries or settings.target_industries,
            "target_roles": req.roles or settings.target_roles,
            "max_leads": req.max_leads or settings.max_leads_per_run,
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

        from pipeline import outreach_graph
        q.put({"type": "status", "message": "Running LangGraph pipeline..."})

        final_state = outreach_graph.invoke(
            initial_state,
            config={"configurable": {"thread_id": run_id}},
        )

        # Restore original log
        helpers_mod.log_agent = original_log

        # Build rich results payload
        results = _build_results_payload(final_state, run_id)
        _runs[run_id]["state"] = results
        _runs[run_id]["status"] = "done"

        q.put({"type": "results", "data": results})
        q.put({"type": "done", "run_id": run_id})

        # Save to disk
        from utils.reporter import save_results
        save_results(final_state)

    except Exception as e:
        import traceback
        err = traceback.format_exc()
        _runs[run_id]["status"] = "error"
        _runs[run_id]["error"] = str(e)
        q.put({"type": "error", "message": str(e), "traceback": err})


def _build_results_payload(state: dict, run_id: str) -> dict:
    """Build a clean JSON payload from pipeline state for the frontend."""
    # Build lead → message lookup
    msg_map: dict[str, dict] = {}
    all_messages = (
        state.get("approved_messages", []) +
        state.get("sent_messages", []) +
        state.get("messages", [])
    )
    for msg in all_messages:
        lid = msg.get("lead_id", "")
        if lid and lid not in msg_map:
            msg_map[lid] = msg

    leads_out = []
    for lead in state.get("researched_leads", []):
        lid = lead.get("id", "")
        msg = msg_map.get(lid, {})

        # Clean pain points
        pain_points = lead.get("pain_points", [])
        if isinstance(pain_points, str):
            pain_points = [p.strip() for p in pain_points.split(";") if p.strip()]

        leads_out.append({
            "id": lid,
            "name": lead.get("name", ""),
            "first_name": lead.get("first_name", ""),
            "title": lead.get("title", ""),
            "company": lead.get("company", ""),
            "company_website": lead.get("company_website", ""),
            "linkedin_url": lead.get("linkedin_url", ""),
            "email": lead.get("email", ""),
            "location": lead.get("location", ""),
            "industry": lead.get("industry", ""),
            "company_size": lead.get("company_size", ""),
            "score": lead.get("score", 0),
            "recommended_service": lead.get("recommended_service", ""),
            "best_channel": lead.get("best_channel", "email"),
            "company_summary": lead.get("company_summary", ""),
            "recent_news": lead.get("recent_news", ""),
            "tech_stack": lead.get("tech_stack", []),
            "pain_points": pain_points,
            "opportunities": lead.get("opportunities", []),
            "project_reference": lead.get("project_reference", ""),
            "message": {
                "channel": msg.get("channel", ""),
                "subject": msg.get("subject", ""),
                "body": msg.get("body", ""),
                "tone_score": msg.get("tone_score", 0),
                "personalization_score": msg.get("personalization_score", 0),
            },
        })

    return {
        "run_id": run_id,
        "stats": {
            "discovered": len(state.get("raw_leads", [])),
            "qualified": len(state.get("filtered_leads", [])),
            "researched": len(state.get("researched_leads", [])),
            "messages_generated": len(state.get("messages", [])),
            "approved": len(state.get("approved_messages", [])),
        },
        "leads": leads_out,
    }
