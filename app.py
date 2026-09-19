"""DroidPerf application extensions.

The existing Flask/NIM application lives in :mod:`app_core`; this module adds
device status and exports. The aggregate function in app_core already includes
FPS stability, RAM in MB, and battery temp alias.
"""
import csv
import io
import json

from flask import Response, jsonify

import app_core
from app_core import aggregate

app = app_core.app


def _session_path(session_id):
    re_pattern = getattr(app_core, "SESSION_ID_RE", getattr(app_core, "_VALID_ID", None))
    if not re_pattern or not re_pattern.fullmatch(session_id):
        return None
    path = app_core.SESSION_DIR / f"{session_id}.json"
    return path if path.is_file() else None


def _unavailable(value):
    return "Measurement unavailable" if value is None else value


@app.get("/connection")
def connection_status():
    """Return the current ADB connection state for the UI."""
    try:
        output = app_core.run_adb(["devices"], timeout=3.0) or ""
    except Exception as exc:
        return jsonify({"connected": False, "state": "error", "message": str(exc)})
    rows = []
    for line in output.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            rows.append({"serial": parts[0], "state": parts[1]})
    connected = any(row["state"] == "device" for row in rows)
    state = "device" if connected else (rows[0]["state"] if rows else "disconnected")
    return jsonify({
        "connected": connected,
        "state": state,
        "devices": rows,
        "message": "ADB connected" if connected else "No usable ADB device",
    })


@app.get("/sessions/<session_id>/export.json")
def export_json(session_id):
    path = _session_path(session_id)
    if path is None:
        return jsonify({"error": "session not found"}), 404
    payload = json.loads(path.read_text(encoding="utf-8"))
    return Response(
        json.dumps(payload, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f"attachment; filename={session_id}.json"},
    )


@app.get("/sessions/<session_id>/export.csv")
def export_csv(session_id):
    path = _session_path(session_id)
    if path is None:
        return jsonify({"error": "session not found"}), 404
    payload = json.loads(path.read_text(encoding="utf-8"))
    samples = payload.get("samples", [])
    fields = sorted({key for sample in samples for key in sample})
    stream = io.StringIO()
    if fields:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for sample in samples:
            writer.writerow({
                key: _unavailable(json.dumps(sample[key]) if isinstance(sample.get(key), (dict, list)) else sample.get(key))
                for key in fields
            })
    else:
        stream.write("Measurement unavailable\n")
    return Response(
        stream.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={session_id}.csv"},
    )


if __name__ == "__main__":
    import logging
    import os
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    port = int(os.environ.get("PORT", "5000"))
    print(f"Starting DroidPerf at http://127.0.0.1:{port}")
    app.run(
        host="127.0.0.1",
        port=port,
        threaded=True,
        use_reloader=False,
    )

