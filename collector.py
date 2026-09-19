"""Public collector API."""
from collector_vsync import (
    GPU_CLOCK_PATHS,
    ROLLING_FRAME_WINDOW,
    SessionCollector,
    _clock,
    _number,
    _frame_columns,
    _frame_rows,
    detect_foreground_app,
    frame_metrics,
    parse_battery,
    parse_cpu_load,
    parse_foreground_app,
    parse_frame_intervals,
    parse_frame_times,
    parse_pss_total,
    parse_thermal,
    run_adb,
)
from collector_adapter import SessionCollector, parse_total_frames, parse_surfaceflinger_stats


def parse_surfaceflinger_fps(output: str, package_name: str):
    """Backwards-compatibility wrapper returning averageFPS."""
    stats = parse_surfaceflinger_stats(output, package_name)
    return stats.get("average_fps") if stats else None


__all__ = [
    "SessionCollector",
    "GPU_CLOCK_PATHS",
    "ROLLING_FRAME_WINDOW",
    "detect_foreground_app",
    "frame_metrics",
    "parse_battery",
    "parse_cpu_load",
    "parse_foreground_app",
    "parse_frame_intervals",
    "parse_frame_times",
    "parse_pss_total",
    "parse_thermal",
    "parse_total_frames",
    "parse_surfaceflinger_stats",
    "parse_surfaceflinger_fps",
    "run_adb",
    "_clock",
    "_number",
]
