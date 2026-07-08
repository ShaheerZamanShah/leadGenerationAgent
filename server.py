"""
server.py
---------
FastAPI backend for the Outreach Agent web app.

Endpoints:
  GET  /                      → serves frontend/index.html (CSS inlined)
  GET  /static/<file>         → serves static frontend assets
  GET  /frontend/<file>       → legacy static asset route
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
from pydantic import BaseModel, Field

# ── Project imports ───────────────────────────────────────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).parent))

from config.settings import settings
from utils.cv_parser import get_cv_summary
from utils.helpers import (
    add_log_listener,
    remove_log_listener,
    set_active_campaign,
    get_active_campaign,
)

app = FastAPI(title="Outreach Agent API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── State store ───────────────────────────────────────────────────────────────
_runs: dict[str, dict] = {}          # run_id → { status, state, queue, ... }
_runs_lock = threading.Lock()
_MAX_FINISHED_RUNS = 40


# ── Request / Response models ─────────────────────────────────────────────────
class CampaignRequest(BaseModel):
    prompt: str = ""
    max_leads: int = Field(default=15, ge=1, le=50)
    no_review: bool = True
    dry_run: bool = True


# ── Static files ──────────────────────────────────────────────────────────────
FRONTEND_DIR = Path(__file__).parent / "frontend"
FRONTEND_DIR.mkdir(exist_ok=True)
_FRONTEND_ROOT = FRONTEND_DIR.resolve()

_STATIC_MEDIA = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def _safe_frontend_path(filename: str) -> Path | None:
    """Resolve a frontend asset path and reject directory traversal."""
    candidate = (_FRONTEND_ROOT / filename).resolve()
    if not str(candidate).startswith(str(_FRONTEND_ROOT)):
        return None
    return candidate


def _inject_stylesheet(html: str) -> str:
    """Inline dashboard CSS so styling survives blocked/missed asset requests."""
    css_file = FRONTEND_DIR / "styles.css"
    if not css_file.exists() or "</head>" not in html:
        return html
    css = css_file.read_text(encoding="utf-8")
    return html.replace("</head>", f'<style id="app-theme">{css}</style>\n</head>', 1)


@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(
            content=_inject_stylesheet(index_file.read_text(encoding="utf-8")),
            headers={"Cache-Control": "no-cache"},
        )
    return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)


@app.api_route("/frontend/{filename:path}", methods=["GET", "HEAD"])
async def serve_static_legacy(filename: str):
    """Backward-compatible asset route used by older deployments/bookmarks."""
    file_path = _safe_frontend_path(filename)
    if file_path and file_path.is_file():
        media_type = _STATIC_MEDIA.get(file_path.suffix.lower())
        return FileResponse(
            str(file_path),
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=3600"},
        )
    raise HTTPException(status_code=404, detail="File not found")


# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    missing = settings.validate()
    return {
        "status": "ok",
        "version": "3.0.0",
        "llm_ready": not bool(missing),
        "missing_keys": missing,
        "apify": bool(settings.apify_api_key),
        "apollo": bool(settings.apollo_api_key),
    }


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


def _prune_old_runs() -> None:
    """Drop oldest finished runs so memory does not grow forever."""
    finished = [
        (rid, r) for rid, r in _runs.items()
        if r.get("status") in ("done", "error", "cancelled")
    ]
    if len(finished) <= _MAX_FINISHED_RUNS:
        return
    finished.sort(key=lambda item: item[1].get("finished_at", 0))
    for rid, _ in finished[: len(finished) - _MAX_FINISHED_RUNS]:
        _runs.pop(rid, None)


def _cancel_active_runs(reason: str = "Superseded by a new campaign") -> list[str]:
    """
    Mark every starting/running campaign as cancelled so a new one can start.
    The old pipeline thread may keep finishing in the background, but it will
    not emit to SSE or overwrite the new run once cancelled.
    """
    cancelled: list[str] = []
    for rid, run in list(_runs.items()):
        if run.get("status") not in ("starting", "running"):
            continue
        cancel_event: threading.Event = run.setdefault("cancel", threading.Event())
        cancel_event.set()
        run["status"] = "cancelled"
        run["error"] = reason
        run["finished_at"] = time.time()
        remove_log_listener(rid)
        try:
            run["queue"].put({
                "type": "error",
                "message": reason,
            })
        except Exception:
            pass
        cancelled.append(rid)
    if get_active_campaign() in cancelled or not cancelled:
        # Clear active pointer if it pointed at a cancelled run
        active = get_active_campaign()
        if active in cancelled:
            set_active_campaign(None)
    return cancelled


# ── Campaign endpoints ────────────────────────────────────────────────────────
@app.post("/api/start-campaign")
async def start_campaign(req: CampaignRequest):
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    missing = settings.validate()
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Missing required API keys: {', '.join(missing)}",
        )

    run_id = str(uuid.uuid4())[:8]
    event_queue: queue.Queue = queue.Queue()
    cancel_event = threading.Event()

    with _runs_lock:
        superseded = _cancel_active_runs()
        _prune_old_runs()
        _runs[run_id] = {
            "status": "starting",
            "queue": event_queue,
            "state": None,
            "error": None,
            "started_at": time.time(),
            "finished_at": None,
            "cancel": cancel_event,
            "superseded": superseded,
        }

    # Per-run overrides (do not mutate global settings — concurrent runs would race)
    run_opts = {
        "human_in_loop": False if req.no_review else settings.human_in_loop,
        "max_leads": req.max_leads or settings.max_leads_per_run,
        "dry_run": req.dry_run,
    }

    thread = threading.Thread(
        target=_run_pipeline,
        args=(run_id, req, event_queue, run_opts),
        daemon=True,
        name=f"pipeline-{run_id}",
    )
    thread.start()

    payload: dict[str, Any] = {"run_id": run_id, "status": "started"}
    if superseded:
        payload["superseded_runs"] = superseded
    return payload


@app.post("/api/cancel-campaign/{run_id}")
async def cancel_campaign(run_id: str):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Run not found")
    with _runs_lock:
        run = _runs[run_id]
        if run.get("status") not in ("starting", "running"):
            return {"run_id": run_id, "status": run.get("status"), "cancelled": False}
        cancel_event: threading.Event = run.setdefault("cancel", threading.Event())
        cancel_event.set()
        run["status"] = "cancelled"
        run["error"] = "Cancelled by user"
        run["finished_at"] = time.time()
        remove_log_listener(run_id)
        if get_active_campaign() == run_id:
            set_active_campaign(None)
        try:
            run["queue"].put({"type": "error", "message": "Campaign cancelled"})
        except Exception:
            pass
    return {"run_id": run_id, "status": "cancelled", "cancelled": True}


@app.get("/api/stream/{run_id}")
async def stream_events(run_id: str):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Run not found")

    def event_generator():
        run = _runs[run_id]
        q: queue.Queue = run["queue"]
        # If client reconnects after completion, replay terminal payload
        if run["status"] in ("done", "error", "cancelled"):
            if run.get("state") and run["status"] == "done":
                yield f"data: {json.dumps({'type': 'results', 'data': run['state']}, default=str)}\n\n"
            if run["status"] in ("error", "cancelled"):
                yield f"data: {json.dumps({'type': 'error', 'message': run.get('error') or 'Pipeline failed'})}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'done', 'run_id': run_id})}\n\n"
            return

        idle_pings = 0
        while True:
            try:
                event = q.get(timeout=15)
                yield f"data: {json.dumps(event, default=str)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
                idle_pings = 0
            except queue.Empty:
                idle_pings += 1
                # Keepalive so proxies / browsers don't drop the stream
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"
                # Safety: if status flipped but queue missed terminal event
                status = _runs.get(run_id, {}).get("status")
                if status == "done":
                    if run.get("state"):
                        yield f"data: {json.dumps({'type': 'results', 'data': run['state']}, default=str)}\n\n"
                    yield f"data: {json.dumps({'type': 'done', 'run_id': run_id})}\n\n"
                    break
                if status in ("error", "cancelled"):
                    yield f"data: {json.dumps({'type': 'error', 'message': run.get('error') or 'Pipeline failed'})}\n\n"
                    break
                if idle_pings > 120:  # ~30 min of silence
                    yield f"data: {json.dumps({'type': 'error', 'message': 'Pipeline timed out waiting for events'})}\n\n"
                    break

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/results/{run_id}")
async def get_results(run_id: str):
    if run_id not in _runs:
        raise HTTPException(status_code=404, detail="Run not found")
    run = _runs[run_id]
    if run["status"] == "error":
        raise HTTPException(status_code=500, detail=run.get("error") or "Pipeline failed")
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
        "has_results": bool(run.get("state")),
    }


# ── Pipeline runner (background thread) ───────────────────────────────────────
def _is_cancelled(run_id: str) -> bool:
    run = _runs.get(run_id) or {}
    if run.get("status") == "cancelled":
        return True
    cancel = run.get("cancel")
    return bool(cancel and cancel.is_set())


def _run_pipeline(run_id: str, req: CampaignRequest, q: queue.Queue, run_opts: dict):
    """Run the LangGraph pipeline and push events to the SSE queue."""
    def on_log(entry: dict) -> None:
        if _is_cancelled(run_id) or get_active_campaign() != run_id:
            return
        try:
            q.put({
                "type": "log",
                "agent": entry.get("agent", ""),
                "message": entry.get("message", ""),
                "status": entry.get("status", "info"),
                "timestamp": entry.get("timestamp"),
                "run_id": run_id,
            })
        except Exception:
            pass

    add_log_listener(run_id, on_log)
    set_active_campaign(run_id)

    # Snapshot + temporarily apply per-run settings that agents read from globals
    prev_hil = settings.human_in_loop
    prev_max = settings.max_leads_per_run
    try:
        settings.human_in_loop = run_opts.get("human_in_loop", settings.human_in_loop)
        settings.max_leads_per_run = run_opts.get("max_leads", settings.max_leads_per_run)

        if _is_cancelled(run_id):
            return

        _runs[run_id]["status"] = "running"
        q.put({"type": "status", "message": "Pipeline starting...", "run_id": run_id})
        q.put({
            "type": "log",
            "agent": "Pipeline",
            "message": "Initializing agents...",
            "status": "info",
            "run_id": run_id,
        })

        from state.schema import OutreachState
        initial_state: OutreachState = {
            "run_id": run_id,
            "user_prompt": (req.prompt or "").strip(),
            "brief": {},
            "max_leads": run_opts.get("max_leads") or settings.max_leads_per_run,
            "skip_discovery": False,
            "raw_leads": [],
            "verified_leads": [],
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
        q.put({
            "type": "log",
            "agent": "Pipeline",
            "message": "Running multi-agent pipeline...",
            "status": "info",
            "run_id": run_id,
        })

        final_state = outreach_graph.invoke(
            initial_state,
            config={"configurable": {"thread_id": run_id}},
        )

        # If a newer campaign replaced this one, discard results quietly
        if _is_cancelled(run_id) or get_active_campaign() != run_id:
            return

        results = _build_results_payload(final_state, run_id)
        _runs[run_id]["state"] = results
        _runs[run_id]["status"] = "done"
        _runs[run_id]["finished_at"] = time.time()

        q.put({"type": "results", "data": results, "run_id": run_id})
        q.put({"type": "done", "run_id": run_id})

        try:
            from utils.reporter import save_results
            save_results(final_state)
        except Exception as save_err:
            if not _is_cancelled(run_id) and get_active_campaign() == run_id:
                q.put({
                    "type": "log",
                    "agent": "Pipeline",
                    "message": f"Warning: could not save results to disk: {save_err}",
                    "status": "warn",
                    "run_id": run_id,
                })

    except Exception as e:
        if _is_cancelled(run_id) or get_active_campaign() != run_id:
            return
        import traceback
        err = traceback.format_exc()
        _runs[run_id]["status"] = "error"
        _runs[run_id]["error"] = str(e)
        _runs[run_id]["finished_at"] = time.time()
        q.put({"type": "error", "message": str(e), "traceback": err, "run_id": run_id})
    finally:
        remove_log_listener(run_id)
        # Only restore globals / clear active pointer if we still own the campaign
        if get_active_campaign() == run_id:
            set_active_campaign(None)
            settings.human_in_loop = prev_hil
            settings.max_leads_per_run = prev_max
        # If superseded, the newer run owns settings — do not clobber them.

def _build_results_payload(state: dict, run_id: str) -> dict:
    """Build a clean JSON payload from pipeline state for the frontend."""
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

        pain_points = lead.get("pain_points", [])
        if isinstance(pain_points, str):
            pain_points = [p.strip() for p in pain_points.split(";") if p.strip()]
        elif not isinstance(pain_points, list):
            pain_points = []

        opportunities = lead.get("opportunities", [])
        if not isinstance(opportunities, list):
            opportunities = []

        tech_stack = lead.get("tech_stack", [])
        if not isinstance(tech_stack, list):
            tech_stack = []

        verification = lead.get("verification", {}) or {}
        leads_out.append({
            "id": lid,
            "name": lead.get("name", "") or "",
            "first_name": lead.get("first_name", "") or "",
            "title": lead.get("title", "") or "",
            "company": lead.get("company", "") or "",
            "company_website": lead.get("company_website", "") or "",
            "linkedin_url": lead.get("linkedin_url", "") or "",
            "email": lead.get("email", "") or "",
            "email_source": lead.get("email_source", verification.get("email_source", "")) or "",
            "location": lead.get("location", "") or "",
            "industry": lead.get("industry", "") or "",
            "company_size": lead.get("company_size", "") or "",
            "score": lead.get("score", 0) or 0,
            "verification": {
                "status": verification.get("status", "unverified"),
                "confidence": verification.get("confidence", 0) or 0,
                "domain_live": bool(verification.get("domain_live", False)),
                "email_valid": bool(verification.get("email_valid", False)),
                "linkedin_valid": bool(verification.get("linkedin_valid", False)),
                "checks": verification.get("checks", []) or [],
            },
            "source": lead.get("source", "") or "",
            "source_url": lead.get("source_url", "") or "",
            "fit_reason": lead.get("fit_reason", "") or "",
            "recommended_service": lead.get("recommended_service", "") or "",
            "best_channel": lead.get("best_channel", "email") or "email",
            "company_summary": lead.get("company_summary", "") or "",
            "recent_news": lead.get("recent_news", "") or "",
            "tech_stack": tech_stack,
            "pain_points": pain_points,
            "opportunities": opportunities,
            "project_reference": lead.get("project_reference", "") or "",
            "message": {
                "channel": msg.get("channel", "") or "",
                "subject": msg.get("subject", "") or "",
                "body": msg.get("body", "") or "",
                "tone_score": msg.get("tone_score", 0) or 0,
                "personalization_score": msg.get("personalization_score", 0) or 0,
            },
        })

    verified_count = sum(
        1 for l in leads_out if l["verification"]["status"] in ("verified", "partial")
    )
    brief = state.get("brief", {}) or {}
    return {
        "run_id": run_id,
        "brief": {
            "goal": brief.get("goal", ""),
            "offering_summary": brief.get("offering_summary", ""),
            "target_roles": brief.get("target_roles", []),
            "target_industries": brief.get("target_industries", []),
            "locations": brief.get("locations", []),
        },
        "stats": {
            "discovered": len(state.get("raw_leads", [])),
            "verified": verified_count,
            "qualified": len(state.get("filtered_leads", [])),
            "researched": len(state.get("researched_leads", [])),
            "messages_generated": len(state.get("messages", [])),
            "approved": len(state.get("approved_messages", [])),
        },
        "leads": leads_out,
    }


# Mount static assets last so /api routes keep priority.
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
