"""Fail-soft Android performance metric collector used by DroidPerf."""
from __future__ import annotations
import argparse, json, math, re, subprocess, threading, time, uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any, Callable, Iterable, Optional

GPU_CLOCK_PATHS = ("/sys/class/kgsl/kgsl-3d0/gpuclk", "/sys/kernel/gpu/gpu_clock")
ROLLING_FRAME_WINDOW = 120

def _number(value: str) -> Optional[float]:
    try: return float(value.strip().replace(",", ""))
    except (TypeError, ValueError): return None

def run_adb(args: Iterable[str], timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=timeout, check=False)
        return result.stdout or ""
    except (OSError, subprocess.SubprocessError): return ""

def parse_foreground_app(output: str) -> Optional[str]:
    for line in output.splitlines():
        if "mCurrentFocus" not in line and "mFocusedApp" not in line: continue
        match = re.search(r"\b([A-Za-z][\w]*(?:\.[A-Za-z][\w]*){1,})(?:/[\w.$-]+)?\b", line)
        if match: return match.group(1)
    return None

def detect_foreground_app() -> Optional[str]: return parse_foreground_app(run_adb(["shell", "dumpsys", "window"]))

def _frame_columns(lines: list[str]) -> tuple[Optional[int], Optional[int]]:
    for line in lines:
        if "IntendedVsync" in line and ("FrameCompleted" in line or "SwapBuffers" in line):
            cols = [x.strip() for x in line.split(",")]
            try:
                start = next(i for i, x in enumerate(cols) if "IntendedVsync" in x)
                end_name = "FrameCompleted" if "FrameCompleted" in line else "SwapBuffers"
                return start, next(i for i, x in enumerate(cols) if end_name in x)
            except StopIteration: return None, None
    return None, None

def _frame_rows(output: str) -> list[tuple[float, float]]:
    start, end = _frame_columns(output.splitlines())
    if start is None or end is None: return []
    rows = []
    for line in output.splitlines():
        parts = [x.strip() for x in line.split(",")]
        if len(parts) <= max(start, end) or not re.match(r"^[01],", line.strip()): continue
        intended, complete = _number(parts[start]), _number(parts[end])
        if intended is not None and complete is not None and complete > intended: rows.append((intended, complete))
    return rows

def parse_frame_times(output: str) -> list[float]:
    """Render duration in ms; this measures work per frame, not FPS."""
    return [(done-start)/1_000_000 for start, done in _frame_rows(output) if .1 <= (done-start)/1_000_000 <= 10_000]

def parse_frame_intervals(output: str) -> list[float]:
    """Frame cadence in ms from consecutive IntendedVsync timestamps.

    Android's gfxinfo data is cumulative.  The caller uses only its newest
    window, avoiding a stale app-start history from dominating current FPS.
    """
    timestamps = [row[0] for row in _frame_rows(output)]
    return [(right-left)/1_000_000 for left, right in zip(timestamps, timestamps[1:]) if 1 <= (right-left)/1_000_000 <= 1_000]

def frame_metrics(frame_intervals_ms: Iterable[float]) -> dict[str, Optional[float]]:
    times = [x for x in frame_intervals_ms if isinstance(x, (int, float)) and x > 0]
    if not times:
        return {"fps": None, "avg_fps": None, "low_1_percent_fps": None, "low_10_percent_fps": None, "max_fps": None, "frame_time_stddev_ms": None, "frame_time_variance_ms2": None}
    fps_values, ordered = [1000/x for x in times], sorted(1000/x for x in times)
    def low(p): return mean(ordered[:max(1, math.ceil(len(ordered)*p))])
    dev = pstdev(times) if len(times) > 1 else 0.0
    return {"fps": 1000/mean(times), "avg_fps": mean(fps_values), "low_1_percent_fps": low(.01), "low_10_percent_fps": low(.10), "max_fps": ordered[min(len(ordered)-1, math.floor(len(ordered)*.95))], "frame_time_stddev_ms": dev, "frame_time_variance_ms2": dev**2}

def parse_pss_total(output: str) -> Optional[int]:
    for pattern in (r"TOTAL\s+PSS\s*:\s*([\d,]+)", r"Pss\s+Total\s*:\s*([\d,]+)"):
        match = re.search(pattern, output, re.I)
        if match: return int(match.group(1).replace(",", ""))
    return None

def parse_battery(output: str) -> dict[str, Optional[float]]:
    level = re.search(r"^\s*level:\s*(\d+)", output, re.I|re.M); temp = re.search(r"^\s*temperature:\s*(-?\d+)", output, re.I|re.M)
    return {"battery_level_percent": float(level.group(1)) if level else None, "battery_temperature_c": float(temp.group(1))/10 if temp else None}

def parse_thermal(output: str) -> list[dict[str, Any]]:
    readings = []
    for line in output.splitlines():
        m = re.search(r"([\w./-]+).*?(-?\d+(?:\.\d+)?)\s*(?:C|celsius)?\b", line)
        if m and ("temp" in line.lower() or "thermal" in line.lower()):
            value = _number(m.group(2))
            if value is not None: readings.append({"zone": m.group(1), "temperature_c": value/1000 if value > 200 else value})
    return readings

def parse_cpu_load(previous: Optional[tuple[int, int]], output: str) -> tuple[Optional[float], Optional[tuple[int, int]]]:
    values = [int(x) for x in re.findall(r"\d+", next((x for x in output.splitlines() if x.startswith("cpu ")), ""))]
    if len(values) < 4: return None, previous
    idle, total = values[3], sum(values)
    if previous is None: return None, (idle, total)
    old_idle, old_total = previous; dt, di = total-old_total, idle-old_idle
    return (max(0., min(100., 100*(1-di/dt))) if dt > 0 else None), (idle, total)

def _clock(value: str) -> Optional[float]:
    number = _number(value)
    return number/1000 if number is not None else None

@dataclass
class SessionCollector:
    package_name: Optional[str] = None; interval: float = 1.; output_dir: Path = Path("sessions")
    runner: Callable[[Iterable[str], float], str] = run_adb
    samples: list[dict[str, Any]] = field(default_factory=list); gpu_path: Optional[str] = None
    cpu_paths: list[str] = field(default_factory=list); cpu_extremes: list[float] = field(default_factory=list); gpu_extremes: list[float] = field(default_factory=list); _previous_cpu: Optional[tuple[int, int]] = None

    def prepare(self) -> None:
        if not self.package_name: self.package_name = parse_foreground_app(self.runner(["shell", "dumpsys", "window"]))
        paths = self.runner(["shell", "ls", "/sys/devices/system/cpu"], timeout=5)
        self.cpu_paths = [f"/sys/devices/system/cpu/{core}/cpufreq/scaling_cur_freq" for core in re.findall(r"\bcpu\d+\b", paths)]
        for path in GPU_CLOCK_PATHS:
            if _clock(self.runner(["shell", "cat", path])) is not None: self.gpu_path = path; break

    def poll_once(self) -> dict[str, Any]:
        package = self.package_name or parse_foreground_app(self.runner(["shell", "dumpsys", "window"]))
        gfx = self.runner(["shell", "dumpsys", "gfxinfo", package, "framestats"]) if package else ""
        render_times, intervals = parse_frame_times(gfx)[-ROLLING_FRAME_WINDOW:], parse_frame_intervals(gfx)[-ROLLING_FRAME_WINDOW:]
        metrics, render = frame_metrics(intervals), frame_metrics(render_times)
        metrics["frame_time_stddev_ms"], metrics["frame_time_variance_ms2"], metrics["avg_render_time_ms"] = render["frame_time_stddev_ms"], render["frame_time_variance_ms2"], mean(render_times) if render_times else None
        clocks = {path.split("/")[-3]: _clock(self.runner(["shell", "cat", path])) for path in self.cpu_paths}; current = [x for x in clocks.values() if x is not None]; self.cpu_extremes.extend(current)
        gpu = _clock(self.runner(["shell", "cat", self.gpu_path])) if self.gpu_path else None
        if gpu is not None: self.gpu_extremes.append(gpu)
        load, self._previous_cpu = parse_cpu_load(self._previous_cpu, self.runner(["shell", "cat", "/proc/stat"])); battery = parse_battery(self.runner(["shell", "dumpsys", "battery"]))
        sample = {"timestamp": datetime.now(timezone.utc).isoformat(), "package_name": package, **metrics, "cpu_clocks_mhz": clocks, "cpu_avg_clock_mhz": mean(current) if current else None, "cpu_load_percent": load, "gpu_clock_mhz": gpu, "gpu_supported": self.gpu_path is not None, "ram_pss_kb": parse_pss_total(self.runner(["shell", "dumpsys", "meminfo", package])) if package else None, **battery, "thermal": parse_thermal(self.runner(["shell", "dumpsys", "thermalservice"]))}
        self.samples.append(sample); return sample

    def run(self, stop_event: Optional[threading.Event] = None, on_sample: Optional[Callable[[dict[str, Any]], None]] = None, duration: Optional[float] = None) -> list[dict[str, Any]]:
        self.prepare(); start = time.monotonic(); stop_event = stop_event or threading.Event()
        while not stop_event.is_set() and (duration is None or time.monotonic()-start < duration):
            try:
                sample = self.poll_once()
                if on_sample: on_sample(sample)
            except Exception: pass
            stop_event.wait(self.interval)
        return self.samples

    def session_data(self) -> dict[str, Any]:
        values = lambda key: [s[key] for s in self.samples if s.get(key) is not None]
        levels, ram, temps = values("battery_level_percent"), values("ram_pss_kb"), values("battery_temperature_c")
        return {"session_id": uuid.uuid4().hex, "created_at": datetime.now(timezone.utc).isoformat(), "package_name": self.package_name, "gpu_supported": self.gpu_path is not None, "battery_start_percent": levels[0] if levels else None, "battery_end_percent": levels[-1] if levels else None, "battery_drop_percent": levels[0]-levels[-1] if len(levels)>1 else None, "peak_battery_temperature_c": max(temps, default=None), "peak_ram_pss_kb": max(ram, default=None), "cpu_clock_min_mhz": min(self.cpu_extremes, default=None), "cpu_clock_max_mhz": max(self.cpu_extremes, default=None), "gpu_clock_min_mhz": min(self.gpu_extremes, default=None), "gpu_clock_max_mhz": max(self.gpu_extremes, default=None), "samples": self.samples}

def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Android game performance metrics via ADB."); parser.add_argument("--package"); parser.add_argument("--duration", type=float, default=10); parser.add_argument("--interval", type=float, default=1); parser.add_argument("--output-dir", default="sessions"); parser.add_argument("--once", action="store_true"); args = parser.parse_args(); c = SessionCollector(args.package, args.interval, Path(args.output_dir))
    if args.once: c.prepare(); print(json.dumps(c.poll_once(), indent=2))
    else: c.run(duration=args.duration); print(json.dumps(c.session_data(), indent=2))
    return 0
if __name__ == "__main__": raise SystemExit(main())
