"""Provider-isolated narrative analysis for DroidPerf sessions."""
from __future__ import annotations

import json
import os
import re
from typing import Any

from pathlib import Path

def _load_env() -> None:
    env_path = Path(__file__).resolve().parent / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("'\"")
                    if k:
                        curr = os.getenv(k)
                        if not curr or curr == "your_nvidia_nim_api_key_here" or curr.startswith("your_") or (v and not v.startswith("your_")):
                            os.environ[k] = v
        except OSError: pass

_load_env()

NIM_BASE_URL = os.getenv("NIM_BASE_URL", "https://integrate.api.nvidia.com/v1")
NIM_MODEL = os.getenv("NIM_MODEL_NAME", "nvidia/nemotron-3-ultra-550b-a55b")
REPORT_KEYS = ("verdict", "summary", "bottleneck", "bottleneck_explanation", "stutter_events", "recommendations")


def fallback_report(reason: str = "AI analysis is not configured") -> dict[str, Any]:
    text = "Raw performance statistics are available. " + reason + "."
    return {
        "verdict": text,
        "summary": text,
        "bottleneck": "stable",
        "bottleneck_explanation": reason + ".",
        "stutter_events": [],
        "recommendations": ["Review the FPS lows, frame-time variance, RAM, and temperature trend."],
        "available": False,
    }


def _downsample(samples: list[dict[str, Any]], limit: int = 200) -> list[dict[str, Any]]:
    if len(samples) <= limit: return samples
    indexes = [round(i * (len(samples) - 1) / (limit - 1)) for i in range(limit)]
    return [samples[i] for i in indexes]


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.I).strip()
    cleaned = re.sub(r"```(?:json)?", "", cleaned, flags=re.I).replace("```", "").strip()

    data = None
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    if not isinstance(data, dict):
        for match in re.finditer(r"\{", cleaned):
            start = match.start()
            depth, in_str, esc = 0, False, False
            for idx in range(start, len(cleaned)):
                c = cleaned[idx]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                else:
                    if c == '"':
                        in_str = True
                    elif c == '{':
                        depth += 1
                    elif c == '}':
                        depth -= 1
                        if depth == 0:
                            try:
                                candidate = json.loads(cleaned[start:idx + 1])
                                if isinstance(candidate, dict):
                                    data = candidate
                                    break
                            except json.JSONDecodeError:
                                pass
                            break
            if isinstance(data, dict):
                break

    if not isinstance(data, dict):
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            try:
                data = json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

    if not isinstance(data, dict):
        raise ValueError("Could not parse valid JSON from model response")

    verdict_text = data.get("verdict") or data.get("summary") or data.get("analysis") or data.get("overview") or data.get("description")
    if not verdict_text or not isinstance(verdict_text, str) or not verdict_text.strip():
        verdict_text = data.get("bottleneck_explanation") or "Performance statistics have been analyzed."
    data["verdict"] = str(verdict_text).strip()
    data["summary"] = data["verdict"]

    data.setdefault("stutter_events", [])
    data.setdefault("recommendations", [])
    data.setdefault("bottleneck", "stable")
    data.setdefault("bottleneck_explanation", "")
    data["available"] = True
    return data


def generate_report(session_json: dict[str, Any]) -> dict[str, Any]:
    """Generate the stable report contract; failures degrade to raw stats."""
    api_key = os.getenv("NIM_API_KEY") or os.getenv("NVIDIA_API_KEY")
    if not api_key or api_key == "your_nvidia_nim_api_key_here" or api_key.startswith("your_"):
        return fallback_report("NIM_API_KEY is not configured in .env")
    base_url = os.getenv("NIM_BASE_URL", NIM_BASE_URL)
    primary_model = os.getenv("NIM_MODEL_NAME", NIM_MODEL)
    
    models_to_try = [primary_model]
    if "meta/llama-3.2-11b-vision-instruct" not in models_to_try:
        models_to_try.append("meta/llama-3.2-11b-vision-instruct")
    last_exc = None
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=30.0, max_retries=1)
        context = {"package_name": session_json.get("package_name"), "aggregates": session_json.get("aggregates", {}), "samples": _downsample(session_json.get("samples", []))}
        
        for model in models_to_try:
            try:
                response = client.chat.completions.create(model=model, temperature=0.2, max_tokens=1400, messages=[
                    {"role": "system", "content": "You are an Android game performance analyst. Respond ONLY with valid JSON, no markdown or preamble. Use exactly these fields: verdict (one paragraph), bottleneck (cpu-bound|gpu-bound|thermal-bound|memory-bound|stable), bottleneck_explanation, stutter_events (array of objects with approx_time_seconds and likely_cause), recommendations (array of short actionable strings). Do not invent unsupported measurements."},
                    {"role": "user", "content": json.dumps(context, separators=(",", ":"))},
                ])
                content = response.choices[0].message.content or ""
                return _parse_json(content)
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc: raise last_exc
    except Exception as exc:
        return fallback_report(f"NIM analysis unavailable ({type(exc).__name__})")
