"""
FastAPI + WebSocket Web Server for Sagem MC1000 Live Diagnostics & Flight Recorder.

Features:
- WebSocket endpoint /ws/telemetry for real-time 15-20 Hz live streaming.
- REST API for status, manual blackbox trigger, and captures library.
- Embedded offline-first HTML5 Canvas dark-mode instrument cluster.
- Real-time dual-trace strip chart (RPM & TPS & Battery) for visualizing cruising stutters.
- Capture viewer with pre/post trigger data playback and automated diagnostic summaries.
"""

import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from flight_recorder import CapturedEvent, FlightRecorder
from sagem_mc1000 import SAGEM_DTC_DEFINITIONS, TelemetryFrame

logger = logging.getLogger("sagem.web")


class WebDiagnosticsState:
    """Shared state between background diagnostics poller and web server."""

    def __init__(self, recorder: FlightRecorder, port_name: str, baudrate: int):
        self.recorder = recorder
        self.port_name = port_name
        self.baudrate = baudrate
        self.is_connected = False
        self.loop_hz = 0.0
        self.total_frames = 0
        self.last_frame: Optional[TelemetryFrame] = None
        self.active_websockets: Set[WebSocket] = set()
        self.manual_trigger_requested = False

    async def broadcast_frame(self, frame: TelemetryFrame, loop_hz: float, is_connected: bool) -> None:
        """Broadcast live frame to all connected WebSocket clients."""
        self.last_frame = frame
        self.loop_hz = loop_hz
        self.is_connected = is_connected

        if not self.active_websockets:
            return

        payload = {
            "type": "telemetry",
            "connected": is_connected,
            "hz": round(loop_hz, 1),
            "total_frames": self.total_frames,
            "data": frame.to_dict(),
            "has_fault": frame.has_active_fault,
            "recorder": {
                "is_capturing": self.recorder.is_capturing,
                "buffer_count": len(self.recorder.buffer),
                "captures_count": len(self.recorder.captured_events),
                "active_trigger": self.recorder.active_trigger_type,
            },
        }
        msg = json.dumps(payload)

        # Broadcast to all connected clients
        dead = []
        for ws in list(self.active_websockets):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active_websockets.discard(ws)

    async def broadcast_event(self, event: CapturedEvent) -> None:
        """Broadcast a newly captured blackbox anomaly event."""
        if not self.active_websockets:
            return

        payload = {
            "type": "capture_event",
            "event": {
                "event_id": event.event_id,
                "timestamp": event.timestamp,
                "iso_time": event.iso_time,
                "trigger_type": event.trigger_type,
                "diagnosis_summary": event.diagnosis_summary,
                "csv_name": Path(event.csv_path).name,
                "json_name": Path(event.json_path).name,
                "report_name": Path(event.report_path).name if event.report_path else None,
                "total_frames": event.total_frames,
            },
        }
        msg = json.dumps(payload)
        for ws in list(self.active_websockets):
            try:
                await ws.send_text(msg)
            except Exception:
                pass


def create_app(state: WebDiagnosticsState) -> FastAPI:
    """Build and configure the FastAPI web application."""
    app = FastAPI(
        title="Aprilia Caponord Sagem MC1000 Flight Recorder",
        version="2.0.0",
        docs_url="/docs",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    static_dir = Path(__file__).parent / "static"
    static_dir.mkdir(parents=True, exist_ok=True)

    @app.get("/", response_class=HTMLResponse)
    async def get_index():
        index_file = static_dir / "index.html"
        if not index_file.exists():
            return HTMLResponse("<h1>Loading Caponord Flight Recorder...</h1>")
        with open(index_file, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())

    @app.get("/api/status")
    async def get_status():
        return {
            "port": state.port_name,
            "baudrate": state.baudrate,
            "connected": state.is_connected,
            "hz": round(state.loop_hz, 1),
            "total_frames": state.total_frames,
            "last_frame": state.last_frame.to_dict() if state.last_frame else None,
            "recorder_armed": state.recorder.is_armed,
            "is_capturing": state.recorder.is_capturing,
            "total_captures": len(state.recorder.captured_events),
            "session_csv": state.recorder.session_csv_path.name,
        }

    @app.post("/api/trigger")
    async def trigger_blackbox():
        """Operator manual trigger button."""
        state.recorder.force_manual_trigger(reason="Manual Web UI trigger by rider")
        return {"status": "triggered", "message": "Manual blackbox capture initiated"}

    @app.get("/api/session/download")
    async def download_session_log(filename: Optional[str] = None):
        """Download continuous ride session CSV file."""
        if filename:
            safe_name = Path(filename).name
            target = state.recorder.captures_dir / safe_name
        else:
            target = state.recorder.session_csv_path
        if not target.exists():
            raise HTTPException(status_code=404, detail="Session log file not found")
        return FileResponse(target, filename=target.name, media_type="text/csv")

    @app.get("/api/captures")
    async def list_captures():
        """List all saved captures in ./captures folder."""
        captures = []
        captures_dir = state.recorder.captures_dir
        if captures_dir.exists():
            for json_file in sorted(captures_dir.glob("*.json"), key=os.path.getmtime, reverse=True):
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        csv_file = json_file.with_suffix(".csv")
                        event_id = data.get("event_id", json_file.stem)
                        report_file = json_file.parent / f"{event_id}_report.txt"
                        captures.append({
                            "event_id": event_id,
                            "trigger_type": data.get("trigger_type", "UNKNOWN"),
                            "iso_time": data.get("trigger_iso_time", ""),
                            "diagnosis_summary": data.get("diagnosis_summary", ""),
                            "forensic_metrics": data.get("forensic_metrics", {}),
                            "total_frames": data.get("total_frames", 0),
                            "json_name": json_file.name,
                            "csv_name": csv_file.name if csv_file.exists() else None,
                            "report_name": report_file.name if report_file.exists() else None,
                            "file_size_kb": round(json_file.stat().st_size / 1024, 1),
                        })
                except Exception as e:
                    logger.debug("Error reading capture %s: %s", json_file.name, e)
        return {"captures": captures}

    @app.get("/api/captures/{filename}")
    async def get_capture_content(filename: str):
        """Retrieve full details of a specific capture file (JSON, CSV, or TXT)."""
        safe_name = Path(filename).name
        file_path = state.recorder.captures_dir / safe_name
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Capture file not found")

        if safe_name.endswith(".json"):
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        elif safe_name.endswith(".txt"):
            with open(file_path, "r", encoding="utf-8") as f:
                return PlainTextResponse(f.read())
        else:
            return FileResponse(file_path, filename=safe_name)

    @app.get("/api/captures/{filename}/report")
    async def get_capture_report(filename: str):
        """Retrieve printable forensic diagnostic report for a capture."""
        safe_name = Path(filename).name
        stem = safe_name.replace(".json", "").replace(".csv", "").replace("_report.txt", "")
        report_file = state.recorder.captures_dir / f"{stem}_report.txt"
        if not report_file.exists():
            report_file = state.recorder.captures_dir / safe_name
        if not report_file.exists():
            raise HTTPException(status_code=404, detail="Forensic report not found")
        with open(report_file, "r", encoding="utf-8") as f:
            return PlainTextResponse(content=f.read())

    @app.get("/api/analysis/holistic")
    async def get_holistic_analysis():
        """Generate and return holistic aggregate analysis across all recorded captures."""
        from analyze_session import analyze_aggregate_events, format_holistic_report, load_all_captures
        events = load_all_captures(state.recorder.captures_dir)
        analysis = analyze_aggregate_events(events)
        report = format_holistic_report(analysis, events)
        return {
            "total_events": len(events),
            "analysis": analysis,
            "report": report,
        }

    @app.websocket("/ws/telemetry")
    async def websocket_telemetry(websocket: WebSocket):
        await websocket.accept()
        state.active_websockets.add(websocket)
        logger.info("WebSocket client connected (%d total)", len(state.active_websockets))

        # Send initial state
        try:
            init_msg = {
                "type": "init",
                "port": state.port_name,
                "baud": state.baudrate,
                "connected": state.is_connected,
                "dtc_defs": {k: v["name"] for k, v in SAGEM_DTC_DEFINITIONS.items()},
                "last_frame": state.last_frame.to_dict() if state.last_frame else None,
                "hz": round(state.loop_hz, 1),
                "total_frames": state.total_frames,
            }
            await websocket.send_text(json.dumps(init_msg))

            while True:
                data = await websocket.receive_text()
                try:
                    msg = json.loads(data)
                    if msg.get("action") == "trigger":
                        state.recorder.force_manual_trigger(reason="Manual trigger from WebSocket client")
                except Exception:
                    pass
        except WebSocketDisconnect:
            state.active_websockets.discard(websocket)
            logger.info("WebSocket client disconnected (%d remaining)", len(state.active_websockets))
        except Exception as e:
            state.active_websockets.discard(websocket)
            logger.debug("WebSocket error: %s", e)

    return app
