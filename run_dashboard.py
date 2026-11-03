"""
Dashboard Server Launcher

Runs the FastAPI telemetry and dashboard server using Uvicorn.
Default URL: http://127.0.0.1:8000
"""

import sys
import uvicorn

if __name__ == "__main__":
    port = 8000
    host = "127.0.0.1"
    print("=" * 64)
    print("  TASK ORCHESTRATION ENGINE — LIVE DASHBOARD & TELEMETRY")
    print("=" * 64)
    print(f"  Access dashboard at: http://{host}:{port}")
    print(f"  API Docs available:  http://{host}:{port}/docs")
    print("=" * 64)
    uvicorn.run("api.main:app", host=host, port=port, reload=False, log_level="info")
