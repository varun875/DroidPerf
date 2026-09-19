"""Integration coverage for the device-facing web application."""
import json
import tempfile
import threading
import time
from pathlib import Path

import analysis
import app
import app_core
from collector import parse_surfaceflinger_stats


def test_surfaceflinger_fps_is_scoped_to_package():
    raw = """
layerName = SurfaceView[com.example.game/com.example.game.Main]
totalFrames=120
averageFPS=59.5
layerName = SurfaceView[other.app/other.app.Main]
totalFrames=900
averageFPS=120
"""
    parsed = parse_surfaceflinger_stats(raw, "com.example.game")
    assert parsed["total_frames"] == 120
    assert parsed["average_fps"] == 59.5


def test_fake_adb_runner_surfaceflinger_integration():
    calls = []
    state = {"frames": 100}

    def fake(args, timeout=5.0):
        calls.append(tuple(args))
        if args[:2] == ["dumpsys", "SurfaceFlinger"]:
            state["frames"] += 60
            return f"layerName = SurfaceView[com.game/app]\ntotalFrames={state['frames']}\naverageFPS=60"
        if args[:2] == ["dumpsys", "meminfo"]:
            return "TOTAL PSS: 245678 kB"
        if args[:2] == ["dumpsys", "battery"]:
            return "temperature: 400"
        # Handle batched system reads (single ADB call with echo markers)
        if len(args) >= 2 and args[0] == "shell" and "===DROIDPERF_0===" in str(args[1]):
            output = []
            for part in args[1].split("; "):
                if part.startswith("echo "):
                    output.append(part.split('"')[1])
                elif "scaling_cur_freq" in part:
                    output.append("450000")
                elif "gpuclk" in part:
                    output.append("500000")
                elif part == "cat /proc/stat":
                    output.append("cpu  100 0 200 300 0 0 0 0 0 0")
                elif part == "dumpsys battery":
                    output.append("level: 87\ntemperature: 400")
                elif part.startswith("dumpsys meminfo"):
                    output.append("TOTAL PSS: 245678 kB")
                elif part == "dumpsys thermalservice":
                    output.append("CPU temp: 72000")
                else:
                    output.append("")
            return "\n".join(output)
        return ""

    c = app_core.SessionCollector("com.game", runner=fake, interval=0.01)
    c.prepare()
    first = c.poll_once()
    second = c.poll_once()
    assert first["fps_source"] == "surfaceflinger_timestats"
    assert second["fps"] is not None
    assert second["ram_mb"] is not None
    assert second["battery_temp_c"] == 40.0
    assert calls


def test_aggregation_includes_stability_ram_and_temperature():
    class Collector:
        samples = [
            {"fps": 60, "ram_mb": 200, "battery_temp_c": 38},
            {"fps": 60, "ram_mb": 240, "battery_temp_c": 40},
            {"fps": 60, "ram_mb": 220, "battery_temp_c": 39},
        ]

    result = app.aggregate(Collector())
    assert result["fps_stability_score"] == 100.0
    assert result["peak_ram_mb"] == 240
    assert result["battery_temp_c"] == 39


def test_exports_and_measurement_unavailable():
    with tempfile.TemporaryDirectory() as folder:
        old = app_core.SESSION_DIR
        app_core.SESSION_DIR = Path(folder)
        session_id = "a" * 32
        payload = {"session_id": session_id, "samples": [{"fps": None, "ram_mb": 2}]}
        (Path(folder) / f"{session_id}.json").write_text(json.dumps(payload), encoding="utf-8")
        client = app.app.test_client()
        assert client.get(f"/sessions/{session_id}/export.json").status_code == 200
        csv_response = client.get(f"/sessions/{session_id}/export.csv")
        assert csv_response.status_code == 200
        assert b"Measurement unavailable" in csv_response.data
        app_core.SESSION_DIR = old


def test_connection_endpoint_uses_adb_runner(monkeypatch):
    monkeypatch.setattr(app_core, "run_adb", lambda args, timeout=3.0: "List of devices attached\nABC\tdevice\n")
    response = app.app.test_client().get("/connection")
    assert response.json["connected"] is True
    assert response.json["state"] == "device"


def test_nim_fallback_remains_available(monkeypatch):
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    report = analysis.generate_report({})
    assert report["available"] is False
    assert report["summary"]
