"""DroidPerf's local Flask server and Server-Sent Events API."""
from __future__ import annotations
import json, queue, threading, uuid
from pathlib import Path
from statistics import mean
from typing import Any
from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from collector import GPU_CLOCK_PATHS, SessionCollector, _clock, detect_foreground_app, run_adb

ROOT, SESSION_DIR = Path(__file__).resolve().parent, Path(__file__).resolve().parent / "sessions"
app = Flask(__name__); active: dict[str, dict[str, Any]] = {}

def aggregate(c: SessionCollector) -> dict[str, Any]:
    def values(key): return [s[key] for s in c.samples if s.get(key) is not None]
    fps, low1, low10, ram, temp = values("fps"), values("low_1_percent_fps"), values("low_10_percent_fps"), values("ram_pss_kb"), values("battery_temperature_c")
    return {"sample_count": len(c.samples), "avg_fps": mean(fps) if fps else None, "low_1_percent_fps": mean(low1) if low1 else None, "low_10_percent_fps": mean(low10) if low10 else None, "max_fps": max(values("max_fps"), default=None), "peak_ram_pss_kb": max(ram, default=None), "average_ram_pss_kb": mean(ram) if ram else None, "battery_temperature_c": temp[-1] if temp else None, "peak_battery_temperature_c": max(temp, default=None), "cpu_clock_min_mhz": min(c.cpu_extremes, default=None), "cpu_clock_max_mhz": max(c.cpu_extremes, default=None), "gpu_clock_min_mhz": min(c.gpu_extremes, default=None), "gpu_clock_max_mhz": max(c.gpu_extremes, default=None)}

def fallback_report(): return {"verdict":"Raw performance statistics are available. AI analysis is not configured yet.","bottleneck":"stable","bottleneck_explanation":"Configure NIM_API_KEY for narrative analysis.","stutter_events":[],"recommendations":["Review the FPS lows and frame-time variance in the chart."],"available":False}

def finish(session_id: str):
    state = active[session_id]; data = state["collector"].session_data(); data["session_id"] = session_id; data["aggregates"] = aggregate(state["collector"]); data["report"] = fallback_report(); SESSION_DIR.mkdir(exist_ok=True); (SESSION_DIR / f"{session_id}.json").write_text(json.dumps(data, indent=2), encoding="utf-8"); state["data"], state["done"] = data, True; state["events"].put(None)

@app.get("/")
def index(): return send_from_directory(ROOT, "index.html")
@app.get("/gpu-support")
def gpu_support():
    for path in GPU_CLOCK_PATHS:
        if _clock(run_adb(["shell", "cat", path])) is not None: return jsonify({"gpu_supported":True,"path":path})
    return jsonify({"gpu_supported":False,"path":None})
@app.get("/detect-app")
def detect_app(): return jsonify({"package_name":detect_foreground_app()})
@app.post("/start")
def start():
    body = request.get_json(silent=True) or {}; session_id = uuid.uuid4().hex; state = {"events":queue.Queue(),"stop":threading.Event(),"done":False,"data":None,"collector":SessionCollector((body.get("package_name") or "").strip() or None, output_dir=SESSION_DIR)}; active[session_id] = state
    def worker():
        try: state["collector"].run(state["stop"], state["events"].put)
        finally: finish(session_id)
    threading.Thread(target=worker, daemon=True).start(); return jsonify({"session_id":session_id})
@app.get("/stream/<session_id>")
def stream(session_id: str):
    state = active.get(session_id)
    if not state: return jsonify({"error":"unknown session"}), 404
    @stream_with_context
    def generate():
        while True:
            item = state["events"].get()
            if item is None: yield "event: done\ndata: {}\n\n"; return
            yield f"data: {json.dumps(item)}\n\n"
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control":"no-cache"})
@app.post("/stop/<session_id>")
def stop(session_id: str):
    state = active.get(session_id)
    if not state: return jsonify({"error":"unknown session"}),404
    state["stop"].set()
    while not state["done"]: threading.Event().wait(.02)
    return jsonify(state["data"])
@app.get("/sessions")
def sessions():
    result=[]
    if SESSION_DIR.exists():
        for path in sorted(SESSION_DIR.glob("*.json"),key=lambda p:p.stat().st_mtime,reverse=True):
            try:
                data=json.loads(path.read_text(encoding="utf-8")); result.append({"session_id":data.get("session_id",path.stem),"created_at":data.get("created_at"),"package_name":data.get("package_name"),"avg_fps":data.get("aggregates",{}).get("avg_fps")})
            except (OSError,json.JSONDecodeError): pass
    return jsonify(result)
@app.get("/sessions/<session_id>")
def saved_session(session_id: str):
    path=SESSION_DIR/f"{session_id}.json"
    if not path.exists(): return jsonify({"error":"session not found"}),404
    return jsonify(json.loads(path.read_text(encoding="utf-8")))
if __name__ == "__main__": app.run(host="127.0.0.1",port=5000,threaded=True)
