"""Live FPS adapter with Android SurfaceFlinger fallback."""
from __future__ import annotations

import re
import time
from typing import Any, Iterable, Optional

from collector_vsync import SessionCollector as _BaseSessionCollector
from collector_vsync import _clock


def parse_total_frames(output: str) -> Optional[int]:
    match = re.search(r"Total\s+frames\s+rendered\s*:\s*(\d+)", output, re.I)
    return int(match.group(1)) if match else None


def parse_surfaceflinger_fps(output: str, package_name: str) -> Optional[float]:
    """Find averageFPS in the SurfaceFlinger layer owned by the game."""
    for block in re.split(r"(?=layerName\s*=)", output, flags=re.I):
        if package_name not in block:
            continue
        match = re.search(r"averageFPS\s*=\s*([0-9]+(?:\.[0-9]+)?)", block, re.I)
        if match:
            value = float(match.group(1))
            if 0 <= value <= 240:
                return value
    return None


class SessionCollector(_BaseSessionCollector):
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
            self.runner(["shell", "dumpsys", "gfxinfo", self.package_name, "reset"], timeout=5.0)
            # SurfaceFlinger sees presented OpenGL/Vulkan frames even when
            # gfxinfo does not expose useful app frame counters.
            self.runner(["shell", "dumpsys", "SurfaceFlinger", "--timestats", "-clear", "-enable"], timeout=5.0)

    def poll_once(self) -> dict[str, Any]:
        sample = super().poll_once()
        if not self.package_name:
            return sample
        summary = self.runner(["shell", "dumpsys", "gfxinfo", self.package_name], timeout=5.0)
        total = parse_total_frames(summary)
        now = time.monotonic()
        previous_total, previous_time = self._fps_previous_total, self._fps_previous_time
        self._fps_previous_total, self._fps_previous_time = total, now
        fps = None
        if total is not None:
            elapsed = max(0.05, now - (previous_time or self._fps_started or now))
            frames = total if previous_total is None else max(0, total - previous_total)
            if frames > 0:
                fps = min(240.0, frames / elapsed)
        surface = self.runner(["shell", "dumpsys", "SurfaceFlinger", "--timestats", "-dump"], timeout=8.0)
        surface_fps = parse_surfaceflinger_fps(surface, self.package_name)
        if surface_fps is not None:
            fps = surface_fps
            sample["fps_source"] = "surfaceflinger_timestats"
        elif fps is not None:
            sample["fps_source"] = "gfxinfo_frame_counter"
        else:
            sample["fps_source"] = None
        if fps is None:
            for key in ("fps", "avg_fps", "low_1_percent_fps", "low_10_percent_fps", "max_fps"):
                sample[key] = None
        else:
            sample["fps"] = fps
            sample["avg_fps"] = fps
        return sample

    def run(self, *args: Any, **kwargs: Any):
        try:
            return super().run(*args, **kwargs)
        finally:
            if self.package_name:
                self.runner(["shell", "dumpsys", "SurfaceFlinger", "--timestats", "-disable"], timeout=5.0)
