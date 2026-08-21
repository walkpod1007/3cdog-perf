"""PerfDog-style FPS stability metrics derived from per-frame intervals.

The metric definitions mirror the wording used in PerfDog's public product
documentation; this module intentionally computes them from the
``frame_times_ms`` series that the collector emits (one row per second).
Each top-level function is pure so the unit tests can assert against
known synthetic sequences.

The public surface is intentionally narrow:

* ``summarize(records, duration_s)`` — fold many ``make_record`` rows into
  the FPS Lab summary the UI consumes.

Everything else is implementation detail.

Definition strings are short and explicit. Conflicting reports from other
tools that follow PerfDog should be re-derived under our own wording; we do
not promise bit-identical numbers.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Dict, Iterable, List, Optional, Sequence


CINEMA_FRAME_MS = 1000.0 / 12.0  # 12 FPS cadence baseline (PerfDog)
JANK_MS = 83.33                  # two cinematic frames
BIGJANK_MS = 125.0               # three cinematic frames
WINDOW_10_MIN_S = 600.0
DROP_FPS_THRESHOLD = 8.0
ONE_PERCENT = 0.01
TENTH_PERCENT = 0.001


DEFINITIONS: Dict[str, str] = {
    "jank_count": "單幀耗時 > 前三幀平均×2 且 > 83.33 ms（兩個電影幀）",
    "bigjank_count": "單幀耗時 > 前三幀平均×2 且 > 125 ms（三個電影幀）",
    "jank_per_10min": "jank 次數按 600 秒比例換算（jank / duration_s × 600）",
    "bigjank_per_10min": "bigjank 次數按 600 秒比例換算（bigjank / duration_s × 600）",
    "stutter_pct": "jank 幀累計耗時 ÷ 總時長（純 sum，不乘 100 換算為百分比字串）",
    "avg_fps": "平均 FPS（總幀數 / 總時長）",
    "max_fps": "最高每秒 FPS（由 frame_times_ms 反推之 frame FPS 取最大值）",
    "min_fps": "最低每秒 FPS（由 frame_times_ms 反推之 frame FPS 取最小值）",
    "fps_variance": "FPS 樣本方差（樣本母體 n，n<2 回傳 None）",
    "drop_count": "相鄰秒 FPS 下降 ≥ 8 的次數",
    "one_percent_low": "1% low FPS（幀耗時排序尾部 1% 換算）",
    "one_tenth_low": "0.1% low FPS（幀耗時排序尾部 0.1% 換算）",
}


def _clean_intervals(values: Iterable[Any]) -> List[float]:
    """Filter to positive, finite numbers (ms)."""
    cleaned: List[float] = []
    for value in values:
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number) and number > 0:
            cleaned.append(number)
    return cleaned


def _intervals_from_seconds(records: Sequence[Dict[str, Any]]) -> List[float]:
    """Flatten ``frame_times_ms`` lists across all 1s rows into one stream."""
    intervals: List[float] = []
    for record in records:
        frame_times = record.get("frame_times_ms")
        if isinstance(frame_times, list):
            intervals.extend(_clean_intervals(frame_times))
    return intervals


def _fps_from_intervals(intervals_ms: Sequence[float]) -> Optional[float]:
    if not intervals_ms:
        return None
    total_ms = sum(intervals_ms)
    if total_ms <= 0:
        return None
    return round(1000.0 * (len(intervals_ms)) / total_ms, 2)


def _per_frame_fps(records: Sequence[Dict[str, Any]]) -> List[float]:
    """Per-second FPS values derived from each row's ``frame_times_ms``."""
    per_second: List[float] = []
    for record in records:
        frame_times = record.get("frame_times_ms")
        if not isinstance(frame_times, list) or not frame_times:
            continue
        fps = _fps_from_intervals(_clean_intervals(frame_times))
        if fps is not None:
            per_second.append(fps)
    return per_second


def _jank_decisions(
    intervals_ms: Sequence[float],
) -> List[Dict[str, float]]:
    """Classify each frame using PerfDog's published criteria.

    Returns a list of dicts with the frame's own ``ms`` and ``is_jank`` /
    ``is_bigjank`` flags. The first two frames have no "previous three
    frames" and are skipped, matching PerfDog's behavior.
    """
    decisions: List[Dict[str, float]] = []
    # index i corresponds to interval i (gap between frame i and frame i+1).
    # PerfDog's rule keys off the slow frame itself, which is interval i+1.
    # So decision[i] uses the mean of intervals [i-3..i-1] for the third
    # previous-three-frames average.
    for index in range(3, len(intervals_ms)):
        previous = intervals_ms[index - 3:index]
        baseline = sum(previous) / 3.0
        frame_ms = intervals_ms[index]
        is_jank = frame_ms > baseline * 2.0 and frame_ms > JANK_MS
        is_bigjank = frame_ms > baseline * 2.0 and frame_ms > BIGJANK_MS
        decisions.append(
            {"ms": frame_ms, "is_jank": is_jank, "is_bigjank": is_bigjank}
        )
    return decisions


def jank_counts(intervals_ms: Sequence[float]) -> Dict[str, int]:
    """Return ``{"jank": int, "bigjank": int}`` from a frame-time stream."""
    decisions = _jank_decisions(intervals_ms)
    jank = sum(1 for item in decisions if item["is_jank"])
    bigjank = sum(1 for item in decisions if item["is_bigjank"])
    return {"jank": jank, "bigjank": bigjank}


def stutter_pct(intervals_ms: Sequence[float]) -> Optional[float]:
    """Sum of jank-frame ms divided by total observed ms."""
    decisions = _jank_decisions(intervals_ms)
    jank_ms = sum(item["ms"] for item in decisions if item["is_jank"])
    total_ms = sum(intervals_ms)
    if total_ms <= 0:
        return None
    return round(100.0 * jank_ms / total_ms, 2)


def percentile_low_fps(
    intervals_ms: Sequence[float], pct: float
) -> Optional[float]:
    """Sort frames by duration, take the slowest ``pct`` tail, convert to FPS."""
    cleaned = _clean_intervals(intervals_ms)
    if not cleaned:
        return None
    cleaned.sort()
    # Sort ascending ms => slowest ms are at higher indices. Tail = slowest.
    tail_size = max(1, int(math.ceil(len(cleaned) * pct)))
    tail = cleaned[-tail_size:]
    avg_ms = sum(tail) / len(tail)
    if avg_ms <= 0:
        return None
    return round(1000.0 / avg_ms, 2)


def drop_count(per_second_fps: Sequence[float]) -> int:
    """Count adjacent-second drops of at least ``DROP_FPS_THRESHOLD``."""
    drops = 0
    for prev, curr in zip(per_second_fps, per_second_fps[1:]):
        if prev - curr >= DROP_FPS_THRESHOLD:
            drops += 1
    return drops


def fps_variance(per_second_fps: Sequence[float]) -> Optional[float]:
    """Sample variance of the per-second FPS samples."""
    if len(per_second_fps) < 2:
        return None
    return round(statistics.pvariance(per_second_fps), 2)


def _aggregate_duration(
    records: Sequence[Dict[str, Any]], fallback: Optional[float]
) -> Optional[float]:
    if fallback is not None and fallback > 0:
        return float(fallback)
    elapsed_values = [
        float(record["elapsed_s"])
        for record in records
        if isinstance(record, dict) and isinstance(record.get("elapsed_s"), (int, float))
    ]
    if elapsed_values:
        return max(elapsed_values) - min(elapsed_values)
    return None


def summarize(
    records: Sequence[Dict[str, Any]],
    duration_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Aggregate ``frame_times_ms`` rows into the FPS Lab summary object.

    Returns ``{"metrics": {...}, "definitions": {...}, "frames": <int>,
    "duration_s": <float>}``. Values are ``None`` when the underlying data
    is missing or insufficient; ``definitions`` is the wording we used.
    """
    intervals = _intervals_from_seconds(records)
    observed_duration = _aggregate_duration(records, duration_s)
    if observed_duration is None or observed_duration <= 0:
        observed_duration = (
            sum(intervals) / 1000.0 if intervals else duration_s
        )

    counts = jank_counts(intervals)
    per_second = _per_frame_fps(records)
    avg_fps = _fps_from_intervals(intervals)
    max_fps = max(per_second) if per_second else None
    min_fps = min(per_second) if per_second else None
    var = fps_variance(per_second)
    drops = drop_count(per_second)

    rate_window = WINDOW_10_MIN_S
    if observed_duration and observed_duration > 0:
        scale = rate_window / observed_duration
    else:
        scale = None
    jank_per_10 = (
        round(counts["jank"] * scale, 2) if scale is not None else None
    )
    bigjank_per_10 = (
        round(counts["bigjank"] * scale, 2) if scale is not None else None
    )

    metrics: Dict[str, Any] = {
        "frames": len(intervals),
        "duration_s": round(observed_duration, 3) if observed_duration is not None else None,
        "avg_fps": avg_fps,
        "max_fps": max_fps,
        "min_fps": min_fps,
        "fps_variance": var,
        "jank": counts["jank"],
        "bigjank": counts["bigjank"],
        "jank_per_10min": jank_per_10,
        "bigjank_per_10min": bigjank_per_10,
        "stutter_pct": stutter_pct(intervals),
        "one_percent_low": percentile_low_fps(intervals, ONE_PERCENT),
        "one_tenth_low": percentile_low_fps(intervals, TENTH_PERCENT),
        "drop_count": drops,
    }
    return {"metrics": metrics, "definitions": DEFINITIONS}