"""DroidPerf Flask web interface for the Stage 1 ADB collector."""
from __future__ import annotations

import json
import queue
import threading
import uuid
from pathlib import Path
from statistics import mean
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context

from collector import GPU_CLOCK_PATHS, SessionCollector, _clock, detect_foreground_app, run_adb

ROOT = Path(__file__).resolve().parent
SESSION_DIR = ROOT / "sessions"
app = Flask(__name__)
active: dict[str, dict[str, Any]] = {}


def aggregate(c: SessionCollector) -> dict[str, Any]:
    samples = c.samples
    def vals(key): return [s[key] for s in samples if s.get(key) is not None]
    fps, low1, low10 = vals("fps"), vals("low_1_percent_fps"), vals("low_10_percent_fps")
    ram, battery = vals("ram_pss_kb"), vals("battery_level_percent")
    return {
        "sample_count": len(samples), "avg_fps": mean(fps) if fps else None,
        "low_1_percent_fps": mean(low1) if low1 else None,
        "low_10_percent_fps": mean(low10) if low10 else None,
        "max_fps": max(vals("max_fps"), default=None), "peak_ram_pss_kb": max(ram, default=None),
        "battery_drop_percent": battery[0] - battery[-1] if len(battery) > 1 else None,
        "cpu_clock_min_mhz": min(c.cpu_extremes, default=None), "cpu_clock_max_mhz": max(c.cpu_extremes, default=None),
        "gpu_clock_min_mhz": min(c.gpu_extremes, default=None), "gpu_clock_max_mhz": max(c.gpu_extremes, default=None),
    }


def fallback_report() -> dict[str, Any]:
    return {"verdict": "Raw performance statistics are available. AI analysis is not configured yet.",
            "bottleneck": "stable", "bottleneck_explanation": "Configure NIM_API_KEY for narrative analysis.",
            "stutter_events": [], "recommendations": ["Review FPS lows and frame-time variance in the chart."], "available": False}


def finish(session_id: str) -> None:
    state = active[session_id]
    c: SessionCollector = state["collector"]
    data = c.session_data()
    data["aggregates"] = aggregate(c)
    data["report"] = fallback_report()
    SESSION_DIR.mkdir(exist_ok=True)
    (SESSION_DIR / f"{session_id}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    state["data"], state["done"] = data, True
    state["events"].put(None)


@app.get("/")
def index(): return send_from_directory(ROOT, "index.html")


@app.get("/gpu-support")
def gpu_support():
    for path in GPU_CLOCK_PATHS:
        if _clock(run_adb(["shell", "cat", path])) is not None:
            return jsonify({"gpu_supported": True, "path": path})
    return jsonify({"gpu_supported": False, "path": None})


@app.get("/detect-app")
def detect_app(): return jsonify({"package_name": detect_foreground_app()})


@app.post("/start")
def start():
    body = request.get_json(silent=True) or {}
    session_id = uuid.uuid4().hex
    state = {"events": queue.Queue(), "stop": threading.Event(), "done": False, "data": None}
    state["collector"] = SessionCollector((body.get("package_name") or "").strip() or None, output_dir=SESSION_DIR)
    active[session_id] = state
    def worker():
        try: state["collector"].run(state["stop"], state["events"].put)
        finally: finish(session_id)
    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"session_id": session_id})


@app.get("/stream/<session_id>")
def stream(session_id: str):
    state = active.get(session_id)
    if not state: return jsonify({"error": "unknown session"}), 404
    @stream_with_context
    def generate():
        while True:
            sample = state["events"].get()
            if sample is None:
                yield "event: done\ndata: {}\n\n"; return
            yield f"data: {json.dumps(sample)}\n\n"
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/stop/<session_id>")
def stop(session_id: str):
    state = active.get(session_id)
    if not state: return jsonify({"error": "unknown session"}), 404
    state["stop"].set()
    while not state["done"]: threading.Event().wait(.02)
    return jsonify(state["data"])


@app.get("/sessions")
def sessions():
    result = []
    if SESSION_DIR.exists():
        for path in sorted(SESSION_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                d = json.loads(path.read_text(encoding="utf-8")); a = d.get("aggregates", {})
                result.append({"session_id": d.get("session_id", path.stem), "created_at": d.get("created_at"), "package_name": d.get("package_name"), "avg_fps": a.get("avg_fps")})
            except (OSError, json.JSONDecodeError): pass
    return jsonify(result)


@app.get("/sessions/<session_id>")
def saved_session(session_id: str):
    path = SESSION_DIR / f"{session_id}.json"
    if not path.exists(): return jsonify({"error": "session not found"}), 404
    return jsonify(json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__": app.run(host="127.0.0.1", port=5000, threaded=True)
