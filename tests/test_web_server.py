"""
Unit tests for the FastAPI Web Server and REST API endpoints.
"""

from pathlib import Path
import pytest
import sys

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient
from flight_recorder import FlightRecorder
from web_server import WebDiagnosticsState, create_app


@pytest.fixture
def client(tmp_path):
    recorder = FlightRecorder(captures_dir=str(tmp_path))
    state = WebDiagnosticsState(recorder, port_name="MOCK_ECU", baudrate=10400)
    app = create_app(state)
    return TestClient(app), state, recorder


def test_web_index(client):
    test_client, state, _ = client
    response = test_client.get("/")
    assert response.status_code == 200
    assert "APRILIA CAPONORD" in response.text
    assert "Sagem MC1000" in response.text


def test_api_status(client):
    test_client, state, _ = client
    response = test_client.get("/api/status")
    assert response.status_code == 200
    data = response.json()
    assert data["port"] == "MOCK_ECU"
    assert data["baudrate"] == 10400
    assert "recorder_armed" in data


def test_api_manual_trigger(client):
    test_client, state, recorder = client
    assert recorder.is_capturing is False
    response = test_client.post("/api/trigger")
    assert response.status_code == 200
    assert response.json()["status"] == "triggered"


def test_api_captures_list(client):
    test_client, state, recorder = client
    response = test_client.get("/api/captures")
    assert response.status_code == 200
    data = response.json()
    assert "captures" in data
    assert isinstance(data["captures"], list)


def test_api_session_download(client):
    test_client, state, recorder = client
    response = test_client.get("/api/session/download")
    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "timestamp,elapsed_sec,rpm" in response.text


def test_api_capture_report(client, tmp_path):
    test_client, state, recorder = client
    event_id = "capture_20260913_120000_stutter"
    report_file = Path(recorder.captures_dir) / f"{event_id}_report.txt"
    report_file.write_text("DOSSIER TEST REPORT CONTENT", encoding="utf-8")

    json_file = Path(recorder.captures_dir) / f"{event_id}.json"
    json_file.write_text('{"event_id": "' + event_id + '", "trigger_type": "TRIGGER_D", "forensic_metrics": {"rpm_drop": 300}}', encoding="utf-8")

    # 1. Fetch report directly via /api/captures/{filename}/report
    rep_res = test_client.get(f"/api/captures/{event_id}/report")
    assert rep_res.status_code == 200
    assert "DOSSIER TEST REPORT CONTENT" in rep_res.text

    # 2. Check that /api/captures lists report_name and forensic_metrics
    list_res = test_client.get("/api/captures")
    assert list_res.status_code == 200
    caps = list_res.json()["captures"]
    assert len(caps) == 1
    assert caps[0]["report_name"] == f"{event_id}_report.txt"
    assert caps[0]["forensic_metrics"]["rpm_drop"] == 300


def test_api_holistic_analysis(client):
    test_client, state, recorder = client
    res = test_client.get("/api/analysis/holistic")
    assert res.status_code == 200
    data = res.json()
    assert "total_events" in data
    assert "analysis" in data
    assert "report" in data
