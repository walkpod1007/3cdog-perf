"""Zero-dependency Android performance collector and command line interface."""

import argparse
import csv
import io
import json
import os
import re
import shlex
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, TextIO, Tuple

from .schema import METRIC_FIELDS, make_record, validate_record


DEFAULT_TIMEOUT = 5.0
DEFAULT_INTERVAL = 1.0


class ADBError(RuntimeError):
    """Base error for actionable adb failures."""


class ADBTimeout(ADBError):
    pass


class ADBUnauthorized(ADBError):
    pass


class ADBOffline(ADBError):
    pass


class ADBUnavailable(ADBError):
    pass


def _adb_error(message: str) -> ADBError:
    normalized = message.lower()
    if "unauthorized" in normalized:
        return ADBUnauthorized(
            "Android device is unauthorized; unlock the phone and accept the USB debugging prompt"
        )
    if "offline" in normalized:
        return ADBOffline("Android device is offline; reconnect USB and wait for adb to recover")
    if "no devices/emulators found" in normalized or "device not found" in normalized:
        return ADBUnavailable("Android device is disconnected or unavailable")
    return ADBError(message.strip() or "adb command failed")


class ADBClient:
    def __init__(self, serial: Optional[str] = None, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.serial = serial
        self.timeout = timeout

    def run(self, *args: str, serial: bool = True, timeout: Optional[float] = None) -> str:
        command = ["adb"]
        if serial and self.serial:
            command.extend(["-s", self.serial])
        command.extend(args)
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout if timeout is None else timeout,
                check=False,
            )
        except FileNotFoundError:
            raise ADBUnavailable("adb is not installed or is not on PATH")
        except subprocess.TimeoutExpired:
            raise ADBTimeout("adb timed out after %.1fs: %s" % (self.timeout, " ".join(args)))
        if result.returncode:
            raise _adb_error(result.stderr or result.stdout)
        return result.stdout

    def shell(self, *args: str, timeout: Optional[float] = None) -> str:
        return self.run("shell", *args, timeout=timeout)

    def list_devices(self) -> List[Dict[str, str]]:
        return parse_devices(self.run("devices", "-l", serial=False))


def parse_devices(output: str) -> List[Dict[str, str]]:
    devices: List[Dict[str, str]] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        item = {"serial": parts[0], "state": parts[1]}
        for token in parts[2:]:
            if ":" in token:
                key, value = token.split(":", 1)
                item[key] = value
        devices.append(item)
    return devices


def select_device(
    client: ADBClient,
    requested_serial: Optional[str] = None,
    allow_unready: bool = False,
) -> str:
    """Select a device, optionally allowing a recording to wait for recovery."""
    devices = client.list_devices()
    if requested_serial:
        matches = [device for device in devices if device["serial"] == requested_serial]
        if not matches:
            if allow_unready:
                return requested_serial
            raise ADBUnavailable("Android device %s is not connected" % requested_serial)
        device = matches[0]
    else:
        if not devices:
            raise ADBUnavailable("No Android devices are connected")
        ready = [device for device in devices if device["state"] == "device"]
        if len(ready) > 1:
            raise ADBUnavailable("Multiple Android devices are connected; pass --serial")
        if not ready and len(devices) > 1:
            raise ADBUnavailable("Multiple unavailable Android devices are connected; pass --serial")
        device = ready[0] if ready else devices[0]
    state = device["state"]
    if allow_unready:
        return device["serial"]
    if state == "unauthorized":
        raise ADBUnauthorized(
            "Android device %s is unauthorized; unlock it and accept the USB debugging prompt"
            % device["serial"]
        )
    if state == "offline":
        raise ADBOffline("Android device %s is offline; reconnect USB" % device["serial"])
    if state != "device":
        raise ADBUnavailable("Android device %s is in state %s" % (device["serial"], state))
    return device["serial"]


def status_for_adb_error(error: ADBError) -> str:
    if isinstance(error, ADBUnauthorized):
        return "device_unauthorized"
    if isinstance(error, ADBOffline):
        return "device_offline"
    if isinstance(error, ADBUnavailable):
        return "device_disconnected"
    if isinstance(error, ADBTimeout):
        return "adb_timeout"
    return "device_error"


def parse_proc_stat(output: str) -> Dict[str, Tuple[int, ...]]:
    snapshot: Dict[str, Tuple[int, ...]] = {}
    for line in output.splitlines():
        parts = line.split()
        if not parts or not re.fullmatch(r"cpu\d*", parts[0]):
            continue
        try:
            values = tuple(int(value) for value in parts[1:])
        except ValueError:
            continue
        if len(values) >= 4:
            snapshot[parts[0]] = values
    return snapshot


def cpu_usage(
    before: Dict[str, Tuple[int, ...]], after: Dict[str, Tuple[int, ...]]
) -> Tuple[Optional[float], Optional[Dict[str, float]]]:
    def one(old: Tuple[int, ...], new: Tuple[int, ...]) -> Optional[float]:
        width = min(len(old), len(new))
        deltas = [new[index] - old[index] for index in range(width)]
        # guest and guest_nice are already included in user and nice.
        total = sum(deltas[: min(width, 8)])
        if total <= 0:
            return None
        idle = deltas[3] + (deltas[4] if width > 4 else 0)
        return round(max(0.0, min(100.0, 100.0 * (total - idle) / total)), 2)

    total = one(before["cpu"], after["cpu"]) if "cpu" in before and "cpu" in after else None
    cores: Dict[str, float] = {}
    for name in sorted(after, key=lambda value: (len(value), value)):
        if name == "cpu" or name not in before:
            continue
        value = one(before[name], after[name])
        if value is not None:
            cores[name] = value
    return total, cores or None


def parse_mem_pss_mb(output: str) -> Optional[float]:
    patterns = (
        r"^\s*TOTAL\s+PSS:\s*([\d,]+)\b",
        r"^\s*TOTAL\s+([\d,]+)\b",
    )
    for pattern in patterns:
        match = re.search(pattern, output, re.MULTILINE | re.IGNORECASE)
        if match:
            return round(int(match.group(1).replace(",", "")) / 1024.0, 2)
    return None


def parse_foreground_package(output: str) -> Optional[str]:
    patterns = (
        r"topResumedActivity\s*=\s*ActivityRecord\{[^\n]*?\bu\d+\s+([\w.$-]+)/",
        r"mResumedActivity\s*:\s*ActivityRecord\{[^\n]*?\bu\d+\s+([\w.$-]+)/",
        r"ResumedActivity\s*:\s*ActivityRecord\{[^\n]*?\bu\d+\s+([\w.$-]+)/",
        r"mCurrentFocus\s*=\s*Window\{[^\n]*?\s([\w.$-]+)/",
    )
    for pattern in patterns:
        match = re.search(pattern, output)
        if match:
            return match.group(1)
    return None


def parse_framestats(output: str) -> List[Dict[str, int]]:
    """Parse every complete gfxinfo framestats row across all windows."""
    frames: List[Dict[str, int]] = []
    header: Optional[List[str]] = None
    in_profile = False
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line == "---PROFILEDATA---":
            in_profile = not in_profile
            header = None
            continue
        if not in_profile or not line:
            continue
        cells = [cell.strip() for cell in line.rstrip(",").split(",")]
        if header is None:
            if "IntendedVsync" in cells and "FrameCompleted" in cells:
                header = cells
            continue
        if len(cells) < len(header):
            continue
        try:
            row = {name: int(cells[index]) for index, name in enumerate(header) if cells[index]}
        except ValueError:
            continue
        completed = row.get("FrameCompleted", 0)
        intended = row.get("IntendedVsync", 0)
        # Flags != 0 means the row is not a normal completed application
        # frame.  Long.MAX_VALUE is Android's pending-frame sentinel.
        if (
            row.get("Flags", 0) == 0
            and 0 < completed < 9_000_000_000_000_000_000
            and 0 < intended < 9_000_000_000_000_000_000
        ):
            frames.append(row)
    return frames


def frame_key(frame: Dict[str, int]) -> Tuple[int, int]:
    return (frame.get("FrameTimelineVsyncId", frame.get("IntendedVsync", 0)), frame["FrameCompleted"])


def frame_is_jank(frame: Dict[str, int]) -> bool:
    deadline = frame.get("FrameDeadline", 0)
    if deadline > 0:
        return frame["FrameCompleted"] > deadline
    interval = frame.get("FrameInterval", 16_666_667)
    return frame["FrameCompleted"] - frame["IntendedVsync"] > max(interval, 1)


def incremental_frame_metrics(
    output: str, seen: Set[Tuple[int, int]], elapsed_s: float
) -> Tuple[Optional[float], Optional[int], Optional[float], Set[Tuple[int, int]]]:
    frames = parse_framestats(output)
    keys = {frame_key(frame) for frame in frames}
    new_frames = [frame for frame in frames if frame_key(frame) not in seen]
    if not new_frames:
        return 0.0, 0, 0.0, keys
    duration = max(elapsed_s, 0.001)
    jank = sum(1 for frame in new_frames if frame_is_jank(frame))
    fps = round(len(new_frames) / duration, 2)
    return fps, jank, round(100.0 * jank / len(new_frames), 2), keys


def parse_surface_layer(output: str, package: str) -> Optional[str]:
    """Select a visible app BLAST/SurfaceView layer from SurfaceFlinger --layers."""
    candidates: List[str] = []
    pattern = re.compile(r"^\s*Layer\s+\[\d+\]\s+(.+)$")
    for line in output.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        name = match.group(1).strip()
        lowered = name.lower()
        if package not in name or "background for" in lowered:
            continue
        if "surfaceview" in lowered and "blast" in lowered:
            return name
        if "blast" in lowered or name.startswith("VRI-"):
            candidates.append(name)
    return candidates[0] if candidates else None


def parse_surface_latency(output: str) -> Tuple[Optional[int], List[int]]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if not lines:
        return None, []
    try:
        refresh_ns = int(lines[0].split()[0])
    except (ValueError, IndexError):
        return None, []
    timestamps: List[int] = []
    sentinel = (1 << 63) - 1
    for line in lines[1:]:
        cells = line.split()
        if len(cells) < 3:
            continue
        try:
            actual_present = int(cells[1])
        except ValueError:
            continue
        if 0 < actual_present < sentinel:
            timestamps.append(actual_present)
    return refresh_ns if refresh_ns > 0 else None, sorted(set(timestamps))


def incremental_surface_metrics(
    output: str, seen: Set[int], elapsed_s: float
) -> Tuple[Optional[float], Optional[int], Optional[float], List[float], Set[int]]:
    """Return fps, jank count, jank %, per-frame intervals in ms, and the new seen set.

    The intervals list is consumed by ``frame_times_ms`` so the FPS Lab view
    can replay jank/bigjank analysis on the same raw data the per-second
    proxy used.
    """
    refresh_ns, timestamps = parse_surface_latency(output)
    if refresh_ns is None or not timestamps:
        return None, None, None, [], set()
    keys = set(timestamps)
    new_timestamps = [timestamp for timestamp in timestamps if timestamp not in seen]
    if not new_timestamps:
        return 0.0, 0, 0.0, [], keys
    if not seen and len(new_timestamps) > 1:
        duration = max((new_timestamps[-1] - new_timestamps[0]) / 1_000_000_000.0, 0.001)
        fps = (len(new_timestamps) - 1) / duration
        intervals = [right - left for left, right in zip(new_timestamps, new_timestamps[1:])]
    else:
        duration = max(elapsed_s, 0.001)
        fps = len(new_timestamps) / duration
        prior = max(seen) if seen else None
        sequence = ([prior] if prior is not None else []) + new_timestamps
        intervals = [right - left for left, right in zip(sequence, sequence[1:])]
    # Games may intentionally present every second or third display refresh.
    # A stable 30 FPS cadence on a 60 Hz panel is not itself jank.
    cadence_ns = max(float(refresh_ns), float(statistics.median(intervals))) if intervals else float(refresh_ns)
    jank = sum(1 for gap in intervals if gap > cadence_ns * 1.5)
    denominator = max(len(intervals), 1)
    # Round to 0.01 ms; present timestamps in SurfaceFlinger are ns.
    intervals_ms = [round(gap / 1_000_000.0, 2) for gap in intervals]
    return round(fps, 2), jank, round(100.0 * jank / denominator, 2), intervals_ms, keys


def parse_battery_power_w(current_output: str, voltage_output: str) -> Optional[float]:
    try:
        current = float(re.search(r"-?\d+(?:\.\d+)?", current_output).group(0))  # type: ignore[union-attr]
        voltage = float(re.search(r"-?\d+(?:\.\d+)?", voltage_output).group(0))  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        return None
    # Android battery sysfs reports microamps and microvolts on Pixel devices.
    if abs(voltage) < 100_000:
        voltage *= 1000.0
    if abs(current) < 10_000:
        current *= 1000.0
    return round(abs(current * voltage) / 1_000_000_000_000.0, 3)


def parse_temperature_c(output: str) -> Optional[float]:
    marker = "Current temperatures from HAL:"
    if marker in output:
        # thermalservice prints a cached section first; prefer the live HAL
        # section to avoid graphing a stale duplicate.
        output = output.rsplit(marker, 1)[-1]
    if "Current temperatures from HAL:" in output:
        output = output.split("Current temperatures from HAL:", 1)[1]
        output = output.split("Current cooling devices from HAL:", 1)[0]
    temperatures: List[Tuple[str, int, float]] = []
    pattern = re.compile(
        r"Temperature\{mValue=(-?[\d.]+),\s*mType=(-?\d+),\s*mName=([^,}]+)", re.IGNORECASE
    )
    for match in pattern.finditer(output):
        value = float(match.group(1))
        if -20.0 <= value <= 150.0:
            temperatures.append((match.group(3).strip(), int(match.group(2)), value))
    priorities = (
        lambda name, kind: kind == 3 and name.upper() == "VIRTUAL-SKIN",
        lambda name, kind: name.upper() in ("SKIN", "VIRTUAL-SKIN"),
        lambda name, kind: kind == 2 or name.lower() == "battery",
        lambda name, kind: "soc" in name.lower(),
        lambda name, kind: kind in (0, 1),
    )
    for predicate in priorities:
        for name, kind, value in temperatures:
            if predicate(name, kind):
                return round(value, 2)
    return round(temperatures[0][2], 2) if temperatures else None


def parse_gpu_percent(output: str) -> Optional[float]:
    for line in output.splitlines():
        payload = line.split("=", 1)[-1].strip()
        ratio = re.search(r"\b(\d+)\s*/\s*(\d+)\b", payload)
        if ratio and int(ratio.group(2)) > 0:
            return round(100.0 * int(ratio.group(1)) / int(ratio.group(2)), 2)
        numbers = [float(value) for value in re.findall(r"-?\d+(?:\.\d+)?", payload)]
        if not numbers:
            continue
        if "@" in payload or "%" in payload:
            value = numbers[0]
        elif len(numbers) == 2 and 0 <= numbers[0] <= numbers[1] and numbers[1] > 0:
            value = 100.0 * numbers[0] / numbers[1]
        else:
            value = numbers[0]
        if numbers:
            if 0.0 <= value <= 100.0:
                return round(value, 2)
    return None


def parse_package_uid(output: str) -> Optional[int]:
    match = re.search(r"\buid:(\d+)\b", output)
    return int(match.group(1)) if match else None


def parse_gpu_work(output: str, uid: int) -> Optional[Tuple[int, int]]:
    active = 0
    inactive = 0
    found = False
    for line in output.splitlines():
        cells = line.split()
        if len(cells) != 4 or not all(cell.isdigit() for cell in cells):
            continue
        if int(cells[1]) != uid:
            continue
        active += int(cells[2])
        inactive += int(cells[3])
        found = True
    return (active, inactive) if found else None


def gpu_work_usage(before: Tuple[int, int], after: Tuple[int, int]) -> Optional[float]:
    active = after[0] - before[0]
    inactive = after[1] - before[1]
    total = active + inactive
    if active < 0 or inactive < 0 or total <= 0:
        return None
    return round(100.0 * active / total, 2)


GPU_COMMAND = (
    "for f in /sys/class/kgsl/kgsl-3d0/gpu_busy_percentage "
    "/sys/class/kgsl/kgsl-3d0/gpubusy /sys/class/devfreq/*gpu*/load "
    "/sys/class/devfreq/*gpu*/gpu_busy_percentage /sys/devices/platform/*gpu*/load "
    "/sys/devices/platform/*.mali/utilization; do "
    "if [ -r \"$f\" ]; then printf '%s=' \"$f\"; cat \"$f\"; fi; done"
)


class Collector:
    def __init__(self, adb: ADBClient) -> None:
        self.adb = adb
        self.previous_cpu: Optional[Dict[str, Tuple[int, ...]]] = None
        self.seen_frames: Set[Tuple[int, int]] = set()
        self.surface_layer: Optional[str] = None
        self.seen_surface_timestamps: Set[int] = set()
        self.previous_frame_time: Optional[float] = None
        self.package_uid: Optional[int] = None
        self.previous_gpu_work: Optional[Tuple[int, int]] = None
        self.last_mem_elapsed: Optional[float] = None
        self.last_mem_pss_mb: Optional[float] = None

    def _metric(
        self,
        name: str,
        source: str,
        operation: Callable[[], Any],
        metrics: Dict[str, Any],
        sources: Dict[str, Optional[str]],
        errors: List[str],
    ) -> None:
        try:
            value = operation()
        except ADBError as exc:
            errors.append("%s: %s" % (name, exc))
            metrics[name] = None
            sources[name] = None
            return
        metrics[name] = value
        sources[name] = source if value is not None else None
        if value is None:
            errors.append("%s: unavailable from %s" % (name, source))

    def sample(
        self, session_id: str, elapsed_s: float, package: str, serial: str
    ) -> Dict[str, Any]:
        metrics: Dict[str, Any] = {field: None for field in METRIC_FIELDS}
        sources: Dict[str, Optional[str]] = {}
        errors: List[str] = []

        try:
            activity = self.adb.shell("dumpsys", "activity", "activities")
        except ADBError as exc:
            return make_record(
                session_id,
                elapsed_s,
                package,
                serial,
                status=status_for_adb_error(exc),
                sources={},
                errors=[str(exc)],
            )
        foreground = parse_foreground_package(activity)
        status = "ok" if foreground == package else "app_background"
        if foreground != package:
            errors.append("app is not foreground (foreground=%s)" % (foreground or "unknown"))

        try:
            current_cpu = parse_proc_stat(self.adb.shell("cat", "/proc/stat"))
            if self.previous_cpu:
                cpu_total, cpu_cores = cpu_usage(self.previous_cpu, current_cpu)
                metrics["cpu_total"] = cpu_total
                metrics["cpu_per_core"] = cpu_cores
                sources["cpu_total"] = "/proc/stat" if cpu_total is not None else None
                sources["cpu_per_core"] = "/proc/stat" if cpu_cores is not None else None
            else:
                errors.append("cpu_total: waiting for second /proc/stat snapshot")
                errors.append("cpu_per_core: waiting for second /proc/stat snapshot")
                sources["cpu_total"] = None
                sources["cpu_per_core"] = None
            self.previous_cpu = current_cpu or self.previous_cpu
        except ADBError as exc:
            errors.extend(["cpu_total: %s" % exc, "cpu_per_core: %s" % exc])
            sources.update({"cpu_total": None, "cpu_per_core": None})

        try:
            now = time.monotonic()
            frame_elapsed = now - self.previous_frame_time if self.previous_frame_time is not None else max(elapsed_s, 1.0)
            if self.surface_layer is None:
                layers = self.adb.shell("dumpsys", "SurfaceFlinger", "--layers")
                self.surface_layer = parse_surface_layer(layers, package)
            fps = jank = jank_pct = None
            fps_source: Optional[str] = None
            frame_times_ms: List[float] = []
            if self.surface_layer:
                command = "dumpsys SurfaceFlinger --latency %s" % shlex.quote(self.surface_layer)
                latency = self.adb.shell(command)
                fps, jank, jank_pct, frame_times_ms, surface_keys = incremental_surface_metrics(
                    latency, self.seen_surface_timestamps, frame_elapsed
                )
                # SurfaceFlinger keeps a bounded rolling buffer. Mirror that
                # buffer instead of growing a session-long set.
                self.seen_surface_timestamps = surface_keys
                if fps is not None:
                    fps_source = "SurfaceFlinger --latency cadence proxy"
            if fps is None:
                gfx = self.adb.shell("dumpsys", "gfxinfo", package, "framestats")
                fps, jank, jank_pct, keys = incremental_frame_metrics(gfx, self.seen_frames, frame_elapsed)
                # gfxinfo is a rolling buffer. Keeping only the current keys
                # caps memory use during long recordings while retaining
                # de-duplication.
                self.seen_frames = keys
                fps_source = "dumpsys gfxinfo framestats"
                # The gfxinfo fallback does not emit per-frame intervals in this
                # revision; leave frame_times_ms empty rather than fabricate.
            self.previous_frame_time = now
            metrics.update({"fps": fps, "jank": jank, "jank_pct": jank_pct})
            if frame_times_ms:
                metrics["frame_times_ms"] = frame_times_ms
                sources["frame_times_ms"] = "SurfaceFlinger --latency per-frame intervals"
            sources.update({"fps": fps_source, "jank": fps_source, "jank_pct": fps_source})
        except ADBError as exc:
            self.surface_layer = None
            for name in ("fps", "jank", "jank_pct"):
                errors.append("%s: %s" % (name, exc))
                sources[name] = None

        if self.last_mem_elapsed is None or elapsed_s - self.last_mem_elapsed >= 5.0:
            try:
                mem = parse_mem_pss_mb(self.adb.shell("dumpsys", "meminfo", "--local", package))
                if mem is None:
                    errors.append("mem_pss_mb: unavailable from dumpsys meminfo --local")
                else:
                    self.last_mem_pss_mb = mem
                    self.last_mem_elapsed = elapsed_s
            except ADBError as exc:
                errors.append("mem_pss_mb: %s" % exc)
        metrics["mem_pss_mb"] = self.last_mem_pss_mb
        sources["mem_pss_mb"] = "dumpsys meminfo --local TOTAL PSS (5s cache)" if self.last_mem_pss_mb is not None else None

        try:
            current = self.adb.shell("cat", "/sys/class/power_supply/battery/current_now")
            voltage = self.adb.shell("cat", "/sys/class/power_supply/battery/voltage_now")
            power = parse_battery_power_w(current, voltage)
            metrics["battery_power_w"] = power
            sources["battery_power_w"] = "battery sysfs current_now*voltage_now" if power is not None else None
            if power is None:
                errors.append("battery_power_w: unreadable battery sysfs values")
        except ADBError as exc:
            errors.append("battery_power_w: %s" % exc)
            sources["battery_power_w"] = None

        self._metric(
            "temp_c",
            "dumpsys thermalservice VIRTUAL-SKIN",
            lambda: parse_temperature_c(self.adb.shell("dumpsys", "thermalservice")),
            metrics,
            sources,
            errors,
        )
        try:
            if self.package_uid is None:
                uid_output = self.adb.shell("cmd", "package", "list", "packages", "-U", package)
                self.package_uid = parse_package_uid(uid_output)
            current_work = (
                parse_gpu_work(self.adb.shell("dumpsys", "gpu"), self.package_uid)
                if self.package_uid is not None
                else None
            )
            gpu = gpu_work_usage(self.previous_gpu_work, current_work) if self.previous_gpu_work and current_work else None
            metrics["gpu"] = gpu
            sources["gpu"] = "dumpsys gpu UID work ratio" if gpu is not None else None
            if current_work is not None:
                self.previous_gpu_work = current_work
            if gpu is None:
                errors.append("gpu: waiting for second dumpsys gpu UID snapshot or unavailable")
        except ADBError as exc:
            metrics["gpu"] = None
            sources["gpu"] = None
            errors.append("gpu: %s" % exc)
        return make_record(
            session_id,
            elapsed_s,
            package,
            serial,
            status=status,
            sources=sources,
            errors=errors,
            **metrics
        )


def iter_records(
    collector: Collector,
    session_id: str,
    package: str,
    serial: str,
    duration_s: float,
    interval_s: float = DEFAULT_INTERVAL,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    max_samples: Optional[int] = None,
) -> Iterator[Dict[str, Any]]:
    if duration_s <= 0:
        raise ValueError("duration_s must be positive")
    if interval_s <= 0:
        raise ValueError("interval_s must be positive")
    start = monotonic()
    count = 0
    while max_samples is None or count < max_samples:
        elapsed = monotonic() - start
        if max_samples is None and count > 0 and elapsed >= duration_s:
            break
        try:
            row = collector.sample(session_id, elapsed, package, serial)
        except Exception as exc:
            row = make_record(
                session_id,
                elapsed,
                package,
                serial,
                status="collector_error",
                sources={},
                errors=["%s: %s" % (type(exc).__name__, exc)],
            )
        validate_record(row)
        yield row
        count += 1
        if max_samples is not None and count >= max_samples:
            break
        # Schedule against the session's absolute cadence. An occasional slow
        # source (notably dumpsys meminfo) may overrun one tick, but must not
        # permanently shift every later sample and lower the average rate.
        delay = max(0.0, start + count * interval_s - monotonic())
        if max_samples is None:
            remaining = max(0.0, duration_s - (monotonic() - start))
            delay = min(delay, remaining)
        if delay:
            sleep(delay)


def write_jsonl(records: Iterable[Dict[str, Any]], stream: TextIO) -> int:
    count = 0
    for record in records:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        count += 1
    return count


def new_session_id(package: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", package).strip("-")
    return "%s--%s" % (stamp, slug)


def default_output(session_id: str) -> Path:
    return Path(__file__).resolve().parent.parent / "sessions" / (session_id + ".jsonl")


def cmd_devices(args: argparse.Namespace) -> int:
    devices = ADBClient(timeout=args.adb_timeout).list_devices()
    if not devices:
        print("No Android devices are connected", file=sys.stderr)
        return 1
    for device in devices:
        print("\t".join((device["serial"], device["state"], device.get("model", "-"))))
    return 0


def cmd_packages(args: argparse.Namespace) -> int:
    base = ADBClient(timeout=args.adb_timeout)
    serial = select_device(base, args.serial)
    adb = ADBClient(serial, args.adb_timeout)
    output = adb.shell("pm", "list", "packages", *(tuple() if args.all else ("-3",)))
    packages = sorted(line.split(":", 1)[1] for line in output.splitlines() if line.startswith("package:"))
    for package in packages:
        print(package)
    return 0


def _prepare_collector(
    args: argparse.Namespace, allow_unready: bool = False
) -> Tuple[str, ADBClient, Collector]:
    base = ADBClient(timeout=args.adb_timeout)
    serial = select_device(base, args.serial, allow_unready=allow_unready)
    adb = ADBClient(serial, args.adb_timeout)
    return serial, adb, Collector(adb)


def cmd_probe(args: argparse.Namespace) -> int:
    serial, _adb, collector = _prepare_collector(args)
    session_id = "probe--%s" % new_session_id(args.package)
    # A short pause between two rows gives /proc/stat a valid difference.
    records = iter_records(
        collector,
        session_id,
        args.package,
        serial,
        duration_s=max(args.interval * 2, 0.2),
        interval_s=args.interval,
        max_samples=2,
    )
    last = None
    for last in records:
        pass
    print(json.dumps(last, ensure_ascii=False, indent=2))
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    serial, _adb, collector = _prepare_collector(args, allow_unready=True)
    session_id = args.session_id or new_session_id(args.package)
    output = Path(args.output).expanduser() if args.output else default_output(session_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    records = iter_records(
        collector,
        session_id,
        args.package,
        serial,
        duration_s=args.duration,
        interval_s=args.interval,
    )
    print("recording %s -> %s" % (session_id, output), file=sys.stderr)
    try:
        with output.open("x", encoding="utf-8", buffering=1) as stream:
            count = write_jsonl(records, stream)
    except FileExistsError:
        raise ValueError("output already exists; choose a new --output path: %s" % output)
    print("wrote %d records to %s" % (count, output), file=sys.stderr)
    print(str(output))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="USB Android performance profiler")
    parser.add_argument("--adb-timeout", type=float, default=DEFAULT_TIMEOUT, help="adb timeout in seconds")
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices = subparsers.add_parser("devices", help="list Android devices and authorization state")
    devices.set_defaults(func=cmd_devices)

    packages = subparsers.add_parser("packages", help="list packages installed on a device")
    packages.add_argument("--serial")
    packages.add_argument("--all", action="store_true", help="include system packages")
    packages.set_defaults(func=cmd_packages)

    for name, help_text, handler in (
        ("collect", "record a JSONL performance session", cmd_collect),
        ("probe", "print one representative sample", cmd_probe),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("package")
        command.add_argument("--serial")
        command.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
        if name == "collect":
            command.add_argument("--duration", type=float, default=60.0)
            command.add_argument("--output")
            command.add_argument("--session-id")
        command.set_defaults(func=handler)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "interval", 1.0) <= 0:
        parser.error("--interval must be positive")
    if getattr(args, "duration", 1.0) <= 0:
        parser.error("--duration must be positive")
    try:
        return int(args.func(args))
    except (ADBError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
