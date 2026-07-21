"""Provider-isolated narrative analysis for DroidPerf sessions."""
from __future__ import annotations

import json
import os
import re
from typing import Any

NIM_BASE_URL = os.getenv("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
NIM_MODEL = os.getenv("NIM_MODEL_NAME", "minimaxai/minimax-m3")
REPORT_KEYS = ("verdict", "bottleneck", "bottleneck_explanation", "stutter_events", "recommendations")


def fallback_report(reason: str = "AI analysis is not configured") -> dict[str, Any]:
    return {"verdict": "Raw performance statistics are available. " + reason + ".", "bottleneck": "stable", "bottleneck_explanation": reason + ".", "stutter_events": [], "recommendations": ["Review the FPS lows, frame-time variance, RAM, and temperature trend."], "available": False}


def _downsample(samples: list[dict[str, Any]], limit: int = 200) -> list[dict[str, Any]]:
    if len(samples) <= limit: return samples
    indexes = [round(i * (len(samples) - 1) / (limit - 1)) for i in range(limit)]
    return [samples[i] for i in indexes]


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip(), flags=re.I)
    try: data = json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.S)
        if not match: raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict): raise ValueError("NIM response was not an object")
    data.setdefault("stutter_events", []); data.setdefault("recommendations", [])
    for key in REPORT_KEYS:
        if key not in data: raise ValueError(f"NIM response missing {key}")
    data["available"] = True
    return data


def generate_report(session_json: dict[str, Any]) -> dict[str, Any]:
    """Generate the stable report contract; failures degrade to raw stats."""
    api_key = os.getenv("NIM_API_KEY") or os.getenv("NVIDIA_API_KEY")
    if not api_key: return fallback_report("NIM_API_KEY is not set")
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=NIM_BASE_URL, timeout=45.0)
        context = {"package_name": session_json.get("package_name"), "aggregates": session_json.get("aggregates", {}), "samples": _downsample(session_json.get("samples", []))}
        response = client.chat.completions.create(model=NIM_MODEL, temperature=0.2, max_tokens=1400, messages=[
            {"role": "system", "content": "You are an Android game performance analyst. Respond ONLY with valid JSON, no markdown or preamble. Use exactly these fields: verdict (one paragraph), bottleneck (cpu-bound|gpu-bound|thermal-bound|memory-bound|stable), bottleneck_explanation, stutter_events (array of objects with approx_time_seconds and likely_cause), recommendations (array of short actionable strings). Do not invent unsupported measurements."},
            {"role": "user", "content": json.dumps(context, separators=(",", ":"))},
        ])
        content = response.choices[0].message.content or ""
        return _parse_json(content)
    except Exception as exc:
        return fallback_report(f"NIM analysis unavailable ({type(exc).__name__})")
