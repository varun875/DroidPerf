"""Corrected live-FPS adapter around the defensive collector implementation."""
from __future__ import annotations

import re
import time
from typing import Any, Iterable, Optional

from collector_vsync import *  # noqa: F401,F403 - preserve the tested parser API.
from collector_vsync import SessionCollector as _BaseSessionCollector


def parse_total_frames(output: str) -> Optional[int]:
    """Read gfxinfo's cumulative completed-frame counter."""
    match = re.search(r"Total\s+frames\s+rendered\s*:\s*(\d+)", output, re.I)
    return int(match.group(1)) if match else None


class SessionCollector(_BaseSessionCollector):
    """Use frame-count deltas for live FPS; retain frame timestamps for stutter."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._fps_previous_total: Optional[int] = None
        self._fps_previous_time: Optional[float] = None
        self._fps_started: Optional[float] = None

    def prepare(self) -> None:
        super().prepare()
        self._fps_started = time.monotonic()
        self._fps_previous_total = None
        self._fps_previous_time = None
        if self.package_name:
            # Start from a clean counter so old gameplay cannot contaminate this run.
            self.runner(["shell", "dumpsys", "gfxinfo", self.package_name, "reset"], timeout=5.0)

    def poll_once(self) -> dict[str, Any]:
        sample = super().poll_once()
        if not self.package_name:
            return sample
        summary = self.runner(["shell", "dumpsys", "gfxinfo", self.package_name], timeout=5.0)
        total = parse_total_frames(summary)
        now = time.monotonic()
        previous_total, previous_time = self._fps_previous_total, self._fps_previous_time
        self._fps_previous_total, self._fps_previous_time = total, now
        if total is None:
            # Do not expose the sparse framestats estimate as live FPS.
            for key in ("fps", "avg_fps", "low_1_percent_fps", "low_10_percent_fps", "max_fps"):
                sample[key] = None
            return sample
        start_time = previous_time or self._fps_started or now
        elapsed = max(0.05, now - start_time)
        frames = total if previous_total is None else max(0, total - previous_total)
        fps = min(240.0, frames / elapsed)
        sample["fps"] = fps
        sample["avg_fps"] = fps
        # Low-FPS statistics from framestats are meaningful only when their
        # cadence agrees with the independent completed-frame counter.
        interval_fps = sample.get("fps")
        if previous_total is not None and interval_fps is not None and sample.get("low_10_percent_fps") is not None:
            low_estimate = sample["low_10_percent_fps"]
            if low_estimate and abs(low_estimate - fps) > max(10.0, fps * 0.75):
                sample["low_1_percent_fps"] = None
                sample["low_10_percent_fps"] = None
                sample["max_fps"] = None
        return sample
