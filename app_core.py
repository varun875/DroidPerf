"""DroidPerf local server: Flask + Server-Sent Events.

Serves the single-page UI, drives a per-session :class:`SessionCollector` on a
background thread, and streams live samples to the browser over SSE. Sessions
are persisted to ``sessions/<id>.json`` and re-listed for the history panel.

Design notes
------------
* One SSE consumer per session. A second attach returns 409.
* The recording's lifetime is tied to the client connection: if the stream
  drops (tab closed, network gone) the worker is signalled to stop, so a dead
  tab can never leave an orphaned adb poller running.
* A 15 s SSE keepalive comment prevents proxy/browser idle-timeouts *and* acts
  as the disconnect probe — the write fails the moment the client is gone.
* Finished sessions linger in memory only long enough for an in-flight
  ``/stop`` to read them, then a reaper thread collects them.
* The ``active`` dict is mutated only under ``_lock``.
"""
from __future__ import annotations

import json
import logging
import math
import os
import queue
import re
import threading
import time
import uuid
from pathlib import Path
from statistics import mean
from typing import Any, Optional

from collections import defaultdict

from flask import (
    Flask,
    Response,
    jsonify,
    request,
    send_from_directory,
    session,
    stream_with_context,
)

import analysis
from analysis import generate_report
from collector import (
    GPU_CLOCK_PATHS,
    SessionCollector,
    _clock,
    detect_foreground_app,
    run_adb,
)

# ── paths & constants ─────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent
SESSION_DIR = ROOT / "sessions"

HEARTBEAT_S = 15.0     # SSE keepalive / disconnect-probe cadence
STOP_WAIT_S = 20.0     # how long /stop blocks for the worker to wind down
GRACE_S = 120.0        # how long a finished session lingers before reaping
MAX_PKG_LEN = 255
MAX_QUEUE_SIZE = 500   # bound SSE event queue to prevent unbounded memory growth
STREAM_TIMEOUT_S = 3600  # max SSE stream duration (1 hour)
DEVICE_DISCONNECT_RETRIES = 3  # consecutive empty ADB responses before giving up

_VALID_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
SESSION_ID_RE = _VALID_ID
_VALID_PKG = re.compile(r"^[A-Za-z0-9_.\\-]{1,%d}$" % MAX_PKG_LEN)
_BOOT = time.monotonic()

logger = logging.getLogger("droidperf")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "droidperf-local-secret")

# ── rate limiting ─────────────────────────────────────────────────────────────
_rate_limits: dict[str, list[float]] = defaultdict(list)


def _rate_limit(key: str, max_requests: int, window_s: float) -> bool:
    """Return True if the request is allowed, False if rate limited."""
    now = time.monotonic()
    timestamps = _rate_limits[key]
    while timestamps and now - timestamps[0] > window_s:
        timestamps.pop(0)
    if len(timestamps) >= max_requests:
        return False
    timestamps.append(now)
    return True


def _get_csrf_token() -> str:
    if "csrf_token" not in session:
        session["csrf_token"] = uuid.uuid4().hex
    return session["csrf_token"]


def _check_csrf() -> bool:
    token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
    return bool(token and token == session.get("csrf_token"))

# ── shared mutable state (guard with _lock) ───────────────────────────────────
_lock = threading.Lock()
active: dict[str, dict[str, Any]] = {}

_gpu_cache: Optional[dict[str, Any]] = None
_gpu_lock = threading.Lock()
_reload_lock = threading.Lock()


# ── helpers ───────────────────────────────────────────────────────────────────
def _valid_id(value: Optional[str]) -> bool:
    return bool(value and _VALID_ID.match(value))


def _put_event(state: dict[str, Any], event: Any) -> None:
    """Put an event on the queue, dropping oldest if full to prevent unbounded growth."""
    try:
        state["events"].put(event, timeout=1.0)
    except queue.Full:
        try:
            state["events"].get_nowait()  # drop oldest
        except queue.Empty:
            pass
        try:
            state["events"].put(event, timeout=0.5)
        except queue.Full:
            logger.warning("event queue full, dropping sample")


def _new_state(package: Optional[str]) -> dict[str, Any]:
    return {
        "package": package,
        "collector": SessionCollector(package, output_dir=SESSION_DIR),
        "events": queue.Queue(maxsize=MAX_QUEUE_SIZE),
        "stop": threading.Event(),
        "finished": threading.Event(),
        "finished_at": None,
        "started_at": time.monotonic(),
        "data": None,
        "streamed": False,
    }


def percentile_mean(values: list[float], p: float) -> Optional[float]:
    """Mean of the lowest ``p`` fraction — the '1% low' style metric."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(1, math.ceil(len(ordered) * p))
    return mean(ordered[:k])


def _maybe_prune(session_id: str) -> None:
    """Drop a finished session once its grace window has elapsed."""
    with _lock:
        state = active.get(session_id)
        if (
            state
            and state["finished"].is_set()
            and time.monotonic() - (state["finished_at"] or 0) > GRACE_S
        ):
            active.pop(session_id, None)


def _reaper() -> None:
    """Backstop: collect finished sessions nobody called /stop for."""
    while True:
        time.sleep(20)
        now = time.monotonic()
        with _lock:
            stale = [
                sid
                for sid, s in active.items()
                if s["finished"].is_set() and now - (s["finished_at"] or now) > GRACE_S
            ]
            for sid in stale:
                active.pop(sid, None)
        if stale:
            logger.debug("reaped %d finished session(s)", len(stale))


_reaper_started = False


def _ensure_reaper() -> None:
    global _reaper_started
    if _reaper_started:
        return
    with _lock:
        if _reaper_started:
            return
        _reaper_started = True
        threading.Thread(target=_reaper, daemon=True, name="droidperf-reaper").start()


# ── statistics ────────────────────────────────────────────────────────────────
def _stability_score(samples: list[dict[str, Any]]) -> Optional[float]:
    """Compute FPS stability as a 0-100 score (100 = perfectly stable)."""
    from statistics import pstdev
    fps = [float(s.get("fps")) for s in samples if s.get("fps") is not None]
    if len(fps) < 2 or mean(fps) <= 0:
        return None
    score = 100.0 * (1.0 - pstdev(fps) / mean(fps))
    return round(max(0.0, min(100.0, score)), 1)


def aggregate(collector: SessionCollector) -> dict[str, Any]:
    samples = getattr(collector, "samples", [])

    def vals(key: str) -> list[float]:
        return [s[key] for s in samples if s.get(key) is not None]

    fps = vals("fps")
    ram = vals("ram_pss_kb")
    temp = vals("battery_temperature_c")
    max_vals = vals("max_fps")
    low1_vals = vals("low_1_percent_fps")
    low10_vals = vals("low_10_percent_fps")

    avg_fps = mean(fps) if fps else None
    # Per-sample lows are preferred when present; otherwise derive from fps.
    low1 = mean(low1_vals) if low1_vals else percentile_mean(fps, 0.01)
    low10 = mean(low10_vals) if low10_vals else percentile_mean(fps, 0.10)
    # The adapter reports an instantaneous rate, so per-sample max is usually
    # absent — fall back to the observed ceiling of the fps series (honest),
    # never to the average.
    max_fps = max(max_vals) if max_vals else (max(fps) if fps else None)

    cpu_extremes = getattr(collector, "cpu_extremes", [])
    gpu_extremes = getattr(collector, "gpu_extremes", [])

    # Extended metrics (FPS stability, RAM in MB, battery temp alias)
    ram_mbs = [s["ram_mb"] for s in samples if s.get("ram_mb") is not None]
    temps_c = [s["battery_temp_c"] for s in samples if s.get("battery_temp_c") is not None]

    result = {
        "sample_count": len(samples),
        "avg_fps": avg_fps,
        "low_1_percent_fps": low1,
        "low_10_percent_fps": low10,
        "max_fps": max_fps,
        "peak_ram_pss_kb": max(ram, default=None),
        "average_ram_pss_kb": mean(ram) if ram else None,
        "battery_temperature_c": temp[-1] if temp else None,
        "peak_battery_temperature_c": max(temp, default=None),
        "cpu_clock_min_mhz": min(cpu_extremes, default=None),
        "cpu_clock_max_mhz": max(cpu_extremes, default=None),
        "gpu_clock_min_mhz": min(gpu_extremes, default=None),
        "gpu_clock_max_mhz": max(gpu_extremes, default=None),
        "fps_stability_score": _stability_score(samples),
        "peak_ram_mb": max(ram_mbs) if ram_mbs else (round(max(ram) / 1024, 2) if ram else None),
        "battery_temp_c": temps_c[-1] if temps_c else (temp[-1] if temp else None),
    }
    return result


def _report(data: dict[str, Any]) -> dict[str, Any]:
    """Generate the AI/analysis report, with an optional dev-time hot reload."""
    try:
        if os.environ.get("DROIDPERF_HOT_RELOAD") == "1":
            with _reload_lock:
                import importlib

                importlib.reload(analysis)
                return analysis.generate_report(data)
        return generate_report(data)
    except Exception as exc:  # noqa: BLE001 — report failure must not kill finish()
        logger.exception("report generation failed")
        return analysis.fallback_report(f"analysis unavailable ({type(exc).__name__})")


def finish(session_id: str) -> None:
    state = active.get(session_id)
    if not state:
        return
    try:
        collector = state["collector"]
        data = collector.session_data()
        data["session_id"] = session_id
        data["aggregates"] = aggregate(collector)
        data["report"] = _report(data)

        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        (SESSION_DIR / f"{session_id}.json").write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        state["data"] = data
        logger.info("session %s finished (%d samples)", session_id, len(collector.samples))
    except Exception as exc:  # noqa: BLE001
        logger.exception("finish() failed for %s", session_id)
        state["data"] = {"session_id": session_id, "error": str(exc)}
    finally:
        state["finished_at"] = time.monotonic()
        _put_event(state, None)  # terminal frame for the SSE consumer
        state["finished"].set()


# ── routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.get("/healthz")
def healthz():
    with _lock:
        n = len(active)
    return jsonify({"ok": True, "active_sessions": n, "uptime_s": round(time.monotonic() - _BOOT, 1)})


@app.get("/gpu-support")
def gpu_support():
    global _gpu_cache
    if _gpu_cache is not None and _gpu_cache.get("gpu_supported"):
        return jsonify(_gpu_cache)
    with _gpu_lock:
        if _gpu_cache is not None and _gpu_cache.get("gpu_supported"):
            return jsonify(_gpu_cache)
        for path in GPU_CLOCK_PATHS:
            if _clock(run_adb(["shell", "cat", path])) is not None:
                _gpu_cache = {"gpu_supported": True, "path": path}
                return jsonify(_gpu_cache)
        return jsonify({"gpu_supported": False, "path": None})



@app.get("/detect-app")
def detect_app():
    return jsonify({"package_name": detect_foreground_app()})


@app.post("/start")
def start():
    _ensure_reaper()
    client_ip = request.remote_addr or "unknown"
    if not _rate_limit(f"start:{client_ip}", max_requests=10, window_s=60):
        return jsonify({"error": "rate limit exceeded, try again later"}), 429
    body = request.get_json(silent=True) or {}
    package = (body.get("package_name") or "").strip() or None
    if package and not _VALID_PKG.match(package):
        return jsonify({"error": "invalid package name"}), 400

    session_id = uuid.uuid4().hex
    state = _new_state(package)
    with _lock:
        active[session_id] = state

    def worker() -> None:
        try:
            state["collector"].run(state["stop"], lambda sample: _put_event(state, sample))
        except Exception as exc:  # noqa: BLE001
            logger.exception("collector crashed for %s", session_id)
            _put_event(state, ("error", f"{type(exc).__name__}: {exc}"))
        finally:
            finish(session_id)

    def _check_device_alive() -> bool:
        """Return False if the ADB device appears to be disconnected."""
        try:
            output = run_adb(["devices"], timeout=3.0)
            return "device" in output.split("\n")[1] if len(output.split("\n")) > 1 else False
        except Exception:
            return False

    threading.Thread(target=worker, daemon=True, name=f"collect-{session_id[:8]}").start()
    logger.info("session %s started (package=%s)", session_id, package)
    return jsonify({"session_id": session_id})


@app.get("/stream/<session_id>")
def stream(session_id: str):
    if not _valid_id(session_id):
        return jsonify({"error": "bad session id"}), 400
    state = active.get(session_id)
    if not state:
        return jsonify({"error": "unknown session"}), 404
    if state.get("streamed"):
        return jsonify({"error": "stream already attached"}), 409
    state["streamed"] = True

    @stream_with_context
    def generate():
        stream_start = time.monotonic()
        try:
            while True:
                if time.monotonic() - stream_start > STREAM_TIMEOUT_S:
                    yield "event: timeout\ndata: {}\n\n"
                    return
                try:
                    item = state["events"].get(timeout=HEARTBEAT_S)
                except queue.Empty:
                    # Keepalive comment: the write raises the instant the
                    # client is gone, which is exactly our disconnect signal.
                    yield ": keepalive\n\n"
                    continue

                if item is None:
                    yield "event: done\ndata: {}\n\n"
                    return
                if isinstance(item, tuple) and item and item[0] == "error":
                    yield f"event: error\ndata: {json.dumps({'message': item[1]})}\n\n"
                    continue
                yield f"data: {json.dumps(item, separators=(',', ':'))}\n\n"
        except GeneratorExit:
            raise
        except Exception:  # noqa: BLE001 — client disconnect surfaces here
            pass
        finally:
            # Client gone (or stream ended): never leave an orphaned recorder.
            state["stop"].set()
            _maybe_prune(session_id)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/stop/<session_id>")
def stop(session_id: str):
    if not _valid_id(session_id):
        return jsonify({"error": "bad session id"}), 400
    client_ip = request.remote_addr or "unknown"
    if not _rate_limit(f"stop:{client_ip}", max_requests=20, window_s=60):
        return jsonify({"error": "rate limit exceeded, try again later"}), 429
    state = active.get(session_id)
    if not state:
        return jsonify({"error": "unknown session"}), 404

    state["stop"].set()
    if not state["finished"].wait(timeout=STOP_WAIT_S):
        logger.warning("stop() timed out waiting on worker for %s", session_id)

    data = state.get("data") or {"session_id": session_id, "error": "stopped"}
    with _lock:
        active.pop(session_id, None)
    logger.info("session %s stopped and released", session_id)
    return jsonify(data)


@app.get("/sessions")
def sessions():
    result: list[dict[str, Any]] = []
    if SESSION_DIR.exists():
        for path in sorted(SESSION_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                result.append(
                    {
                        "session_id": data.get("session_id", path.stem),
                        "created_at": data.get("created_at"),
                        "package_name": data.get("package_name"),
                        "avg_fps": data.get("aggregates", {}).get("avg_fps"),
                    }
                )
            except (OSError, json.JSONDecodeError):
                logger.debug("skipping unreadable session file %s", path.name)
    return jsonify(result)


@app.get("/sessions/<session_id>")
def saved_session(session_id: str):
    if not _valid_id(session_id):
        return jsonify({"error": "bad session id"}), 400
    path = SESSION_DIR / f"{session_id}.json"
    if not path.exists():
        return jsonify({"error": "session not found"}), 404

    data = json.loads(path.read_text(encoding="utf-8"))
    aggs = data.get("aggregates", {})

    # Backfill percentile/max for files written before those fields existed.
    if aggs.get("low_1_percent_fps") is None and data.get("samples"):
        fps = [s["fps"] for s in data["samples"] if s.get("fps") is not None]
        if fps:
            aggs["low_1_percent_fps"] = percentile_mean(fps, 0.01)
            aggs["low_10_percent_fps"] = percentile_mean(fps, 0.10)
            if aggs.get("max_fps") is None:
                aggs["max_fps"] = max(fps)
            data["aggregates"] = aggs
            try:
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError:
                logger.debug("could not write back backfilled aggregates to %s", path.name)

    return jsonify(data)


# ── boot ──────────────────────────────────────────────────────────────────────
_ensure_reaper()

if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PORT", "5000")),
        threaded=True,
        use_reloader=False,
    )
