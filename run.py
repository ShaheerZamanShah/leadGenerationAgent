"""
run.py
------
Entry point: starts FastAPI server and opens the browser automatically.
"""

import sys
import os
import subprocess
import time
import webbrowser
import threading

# Force UTF-8 everywhere on Windows
if sys.platform == "win32":
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PORT = 8000
URL = f"http://localhost:{PORT}"


def open_browser():
    """Open browser after a short delay to let the server start."""
    time.sleep(2.5)
    print(f"\n[Outreach Agent] Opening browser at {URL}\n")
    webbrowser.open(URL)


if __name__ == "__main__":
    print("=" * 65)
    print("  Outreach Agent v2.0 — AI/ML Client Discovery & Outreach")
    print("=" * 65)
    print(f"  Server:   {URL}")
    print(f"  API Docs: {URL}/docs")
    print("=" * 65)
    print()

    # Open browser in background thread
    threading.Thread(target=open_browser, daemon=True).start()

    # Start uvicorn
    import uvicorn
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="info",
    )
