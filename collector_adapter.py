"""Live FPS adapter: SurfaceFlinger timestats primary, gfxinfo fallback."""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable, Optional

from collector_vsync import SessionCollector as _BaseSessionCollector

logger = logging.getLogger("droidperf.adapter")

MAX_FPS = 240.0
MIN_POLL_INTERVAL = 0.05
_FPS_KEYS = ("fps", "avg_fps", "low_1_percent_fps", "low_10_percent_fps", "max_fps")


def parse_total_frames(output: str) -> Optional[int]:
    if not output:
        return None
    match = re.search(r"Total\s+frames\s+rendered\s*:\s*(\d+)", output, re.I)
    return int(match.group(1)) if match else None


def parse_surfaceflinger_stats(
    output: str, package_name: str
) -> Optional[dict[str, Optional[float]]]:
    """Read the presented-frame counter and running avg for the game's layer.

    Returns ``{"total_frames": int|None, "average_fps": float|None}`` for the
    first layer whose ``layerName`` actually contains the package, or ``None``
    when the package owns no timestats layer. Matching is scoped to the
    layerName value so a neighbour layer that merely mentions the package in a
    buffer name can't hijack the reading.
    """
    if not output or not package_name:
        return None
    for block in re.split(r"(?=layerName\s*=)", output, flags=re.I):
        name = re.search(r"layerName\s*=\s*\"?([^\n\"]+)\"?", block, re.I)
        if not name or package_name not in name.group(1):
            continue

        tf = re.search(r"totalFrames\s*=\s*(\d+)", block, re.I)
        af = re.search(r"averageFPS\s*=\s*([0-9]+(?:\.[0-9]+)?)", block, re.I)

        total_frames = int(tf.group(1)) if tf else None
        average_fps = float(af.group(1)) if af else None
        if average_fps is not None and not (0 <= average_fps <= MAX_FPS):
            average_fps = None
        # A layer we own but that reports nothing usable yet.
        if total_frames is None and average_fps is None:
            continue
        return {"total_frames": total_frames, "average_fps": average_fps}
    return None


class SessionCollector(_BaseSessionCollector):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        orig_runner = self.runner

        def wrapped_runner(cmd_args: Iterable[str], timeout: float = 5.0) -> str:
            cmd_list = list(cmd_args)
            try:
                res = orig_runner(cmd_list, timeout=timeout) or ""
                if res:
                    return res
            except Exception:
                logger.debug("runner failed for %s", cmd_list, exc_info=True)
                res = ""
            if cmd_list and cmd_list[0] == "shell":
                try:
                    return orig_runner(cmd_list[1:], timeout=timeout) or ""
                except Exception:
                    logger.debug("runner fallback failed for %s", cmd_list[1:], exc_info=True)
            return res

        self.runner = wrapped_runner

        # gfxinfo (fallback) delta state
        self._gfx_previous_total: Optional[int] = None
        self._gfx_previous_time: Optional[float] = None
        # SurfaceFlinger (primary) delta state
        self._sf_previous_total: Optional[int] = None
        self._sf_previous_time: Optional[float] = None
        self._started: Optional[float] = None

    # ── adb helper ────────────────────────────────────────────────────────
    def _shell(self, args: list[str], timeout: float) -> str:
        """Run an adb shell command, returning '' on any failure."""
        try:
            res = self.runner(args, timeout=timeout) or ""
            if res:
                return res
        except Exception:
            logger.debug("shell command failed: %s", args, exc_info=True)
        try:
            return self.runner(["shell", *args], timeout=timeout) or ""
        except Exception:
            logger.debug("shell fallback failed: %s", args, exc_info=True)
            return ""

    # ── lifecycle ─────────────────────────────────────────────────────────
    def prepare(self) -> None:
        super().prepare()
        self._started = time.monotonic()
        self._gfx_previous_total = self._gfx_previous_time = None
        self._sf_previous_total = self._sf_previous_time = None
        if not self.package_name:
            return
        # Reset the fallback counter so a later fall-back starts clean.
        self._shell(["dumpsys", "gfxinfo", self.package_name, "reset"], timeout=5.0)
        # Open the primary accumulation window.
        self._shell(
            ["dumpsys", "SurfaceFlinger", "--timestats", "-clear", "-enable"],
            timeout=5.0,
        )

    def run(self, *args: Any, **kwargs: Any):
        try:
            return super().run(*args, **kwargs)
        finally:
            if self.package_name:
                self._shell(
                    ["dumpsys", "SurfaceFlinger", "--timestats", "-disable"],
                    timeout=5.0,
                )

    # ── per-source readers (both stateful, both instantaneous) ────────────
    def _surfaceflinger_fps(self) -> Optional[float]:
        dump = self._shell(
            ["dumpsys", "SurfaceFlinger", "--timestats", "-dump"], timeout=8.0
        )
        stats = parse_surfaceflinger_stats(dump, self.package_name)
        if stats is None:
            return None

        now = time.monotonic()
        total = stats["total_frames"]

        # Advance the baseline whenever the counter is readable, even on a
        # zero-delta tick, so a pause doesn't poison the next interval's rate.
        if total is not None:
            prev_total = self._sf_previous_total
            prev_time = self._sf_previous_time or self._started or now
            self._sf_previous_total = total
            self._sf_previous_time = now

            elapsed = max(MIN_POLL_INTERVAL, now - prev_time)
            frames = total if prev_total is None else max(0, total - prev_total)
            if frames > 0:
                return min(MAX_FPS, frames / elapsed)
            # Counter readable but no new frames this tick: no measurement.
            return None

        # Counter missing entirely: fall back to SF's own running average.
        return stats["average_fps"]

    def _gfxinfo_fps(self) -> Optional[float]:
        summary = self._shell(["dumpsys", "gfxinfo", self.package_name], timeout=5.0)
        total = parse_total_frames(summary)
        if total is None:
            # Parse failed: do NOT move the baseline, or the next good read
            # would be mistaken for the first sample and spike.
            return None

        now = time.monotonic()
        prev_total = self._gfx_previous_total
        prev_time = self._gfx_previous_time or self._started or now
        self._gfx_previous_total = total
        self._gfx_previous_time = now

        elapsed = max(MIN_POLL_INTERVAL, now - prev_time)
        frames = total if prev_total is None else max(0, total - prev_total)
        if frames <= 0:
            return None
        return min(MAX_FPS, frames / elapsed)

    # ── polling ───────────────────────────────────────────────────────────
    def poll_once(self) -> dict[str, Any]:
        sample = super().poll_once()

        if sample.get("ram_pss_kb") is not None:
            sample["ram_mb"] = sample["ram_pss_kb"] / 1024.0
        elif sample.get("ram_mb") is None:
            sample["ram_mb"] = None

        if "battery_temperature_c" in sample:
            sample["battery_temp_c"] = sample.get("battery_temperature_c")

        if not self.package_name:
            return sample

        fps: Optional[float] = None
        source: Optional[str] = None

        # Primary: SurfaceFlinger presented-frame counter (sees Vulkan / custom
        # render loops gfxinfo misses). Fallback: gfxinfo, consulted only when
        # SF has nothing — so the cheaper call is skipped on the happy path.
        sf = self._surfaceflinger_fps()
        if sf is not None:
            fps, source = sf, "surfaceflinger_timestats"
        else:
            gfx = self._gfxinfo_fps()
            if gfx is not None:
                fps, source = gfx, "gfxinfo_frame_counter"

        sample["fps_source"] = source
        self._apply_fps(sample, fps)
        return sample

    @staticmethod
    def _apply_fps(sample: dict[str, Any], fps: Optional[float]) -> None:
        if fps is None:
            for key in _FPS_KEYS:
                sample[key] = None
            return
        # Instantaneous rate only — no frame-time distribution is available
        # from either source, so the percentile/max keys stay honestly null.
        sample["fps"] = fps
        sample["avg_fps"] = fps
        sample["low_1_percent_fps"] = None
        sample["low_10_percent_fps"] = None
        sample["max_fps"] = None
