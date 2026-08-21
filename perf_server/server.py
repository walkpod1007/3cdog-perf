#!/usr/bin/env python3
"""Zero-dependency local report server for Android Perf sessions.

The server is a tiny stdlib HTTP front-end for the JSONL sessions emitted by
``src/collector.py``. It is intentionally read-only on the filesystem except
for a single privilege: spawning a single child collector at a time for the
UI's "● 錄製" affordance (see ``RecordController``).

The runtime guarantees are:

* The server only listens on ``127.0.0.1`` (configurable but never public).
* Only one collector child can run at a time. ``/api/record/start`` returns
  HTTP 409 if a previous session is still live.
* ``/api/record/stop`` always tears the subprocess down, even when the UI
  page closes mid-recording.
* On startup we sweep any orphan ``android_perf.py collect ...`` children
  so the server does not leak processes across restarts.
* ``/api/foreground`` is the one read-only exception: it shells out to
  ``adb shell dumpsys activity activities`` to report the current foreground
  package so the UI can auto-select it. It never writes to the device.
"""

import argparse
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse


WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_CHUNK_LINES = 5000
MAX_LINE_BYTES = 2 * 1024 * 1024

# ``WEB_DIR`` resolves to ``<package>/web`` so the static assets stay
# versioned alongside the Python source. ``PROJECT_ROOT`` points at the
# parent of the perf_server package (i.e. the checkout root) so the spawn
# logic below can locate the legacy ``android_perf.py`` stub when running
# from an editable install. When the package is shipped via Homebrew the
# collector is invoked through the ``3cdog-perf-collect`` console script and
# the legacy path is never read.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ANDROID_PERF_SCRIPT = PROJECT_ROOT / "android_perf.py"  # legacy path; unused in the packaged build

# Console-script-style entry used to spawn the Android collector. Resolved
# lazily so ``3cdog-perf --version`` does not pay for ``shutil.which``.
COLLECT_SCRIPT = "3cdog-perf-collect"

# iOS bundle-id -> friendly display name table (Phase 2 task brief: "pikmin
# 等級的常見款 + fallback 顯示 bundle id"). Kept here as the single source of
# truth so this server stays a zero-dependency stdlib HTTP front-end; the
# front-end mirrors the same table for its fallback rendering.
BUNDLE_FRIENDLY_NAMES = {
    "com.nianticlabs.pikmin": "Pikmin",
    "com.nianticlabs.pikminbloom": "Pikmin Bloom",
    "com.google.ios.youtube": "YouTube",
    "com.netflix.Netflix": "Netflix",
    "com.amazon.avod.thirdpartyclient": "Prime Video",
    "com.apple.mobilesafari": "Safari",
    "com.hbo.hboMax": "Max",
    "com.disney.disneyplus": "Disney+",
    "com.spotify.client": "Spotify",
}


def friendly_name(bundle_id):
    """Return the friendly app name for an iOS bundle id, or the raw id."""
    if not bundle_id:
        return "unknown"
    return BUNDLE_FRIENDLY_NAMES.get(bundle_id, bundle_id)


# iOS source marker we stash inside the ``sources`` dict on each converted
# row. Public so the front-end can group sessions by source.
IOS_SOURCE_MARKER = "ios"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_session(sessions_dir: Path, name: str) -> Path:
    """Resolve a client-provided JSONL name without permitting traversal."""
    if not name or "\x00" in name or "\\" in name:
        raise ValueError("invalid session name")
    candidate_name = Path(unquote(name))
    if candidate_name.is_absolute() or candidate_name.suffix.lower() != ".jsonl":
        raise ValueError("session must be a relative .jsonl path")
    root = sessions_dir.resolve()
    candidate = (root / candidate_name).resolve()
    if not _is_within(candidate, root):
        raise ValueError("session path escapes sessions directory")
    return candidate


def _read_json_line(raw: bytes) -> Tuple[Optional[Dict[str, object]], Optional[str]]:
    if len(raw) > MAX_LINE_BYTES:
        return None, "line exceeds 2 MiB limit"
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, "invalid JSONL row: {}".format(exc)
    if not isinstance(value, dict):
        return None, "JSONL row is not an object"
    return value, None


def read_session_chunk(path: Path, offset: int, limit: int) -> Dict[str, object]:
    """Read complete JSONL rows from byte offset; tolerate an in-progress last row."""
    size = path.stat().st_size
    if offset < 0 or offset > size:
        raise ValueError("offset is outside session file")
    limit = max(1, min(limit, MAX_CHUNK_LINES))
    records: List[Dict[str, object]] = []
    warnings: List[str] = []
    next_offset = offset
    with path.open("rb") as handle:
        handle.seek(offset)
        for _ in range(limit):
            row_start = handle.tell()
            raw = handle.readline(MAX_LINE_BYTES + 1)
            if not raw:
                break
            # The collector flushes complete rows. If a writer is between bytes,
            # keep the cursor before the partial row and retry on the next poll.
            if not raw.endswith(b"\n") and handle.tell() == size:
                handle.seek(row_start)
                break
            next_offset = handle.tell()
            record, warning = _read_json_line(raw)
            if record is not None:
                records.append(record)
            if warning:
                warnings.append("byte {}: {}".format(row_start, warning))
    current_size = path.stat().st_size
    return {
        "records": records,
        "next_offset": next_offset,
        "file_size": current_size,
        "eof": next_offset >= current_size,
        "warnings": warnings,
    }


def _first_and_last(path: Path) -> Tuple[Optional[Dict[str, object]], Optional[Dict[str, object]]]:
    first = None
    last = None
    try:
        with path.open("rb") as handle:
            for raw in handle:
                record, _ = _read_json_line(raw)
                if record is not None:
                    if first is None:
                        first = record
                    last = record
    except OSError:
        pass
    return first, last


def list_sessions(sessions_dir: Path) -> List[Dict[str, object]]:
    root = sessions_dir.resolve()
    sessions: List[Dict[str, object]] = []
    if not root.exists():
        return sessions
    for path in root.rglob("*.jsonl"):
        try:
            resolved = path.resolve()
            if not path.is_file() or not _is_within(resolved, root):
                continue
            stat = path.stat()
            first, last = _first_and_last(path)
        except OSError:
            continue
        first = first or {}
        last = last or first
        # ``sources.platform`` is set by the iOS converter that ships in a
        # future release; anything missing that key is treated as Android.
        # Kept out of the canonical RECORD_FIELDS so Android rows don't have to
        # carry the key — we surface it to the UI purely as a display grouping.
        sources = last.get("sources") if isinstance(last.get("sources"), dict) else {}
        first_sources = first.get("sources") if isinstance(first.get("sources"), dict) else {}
        platform = sources.get("platform") or first_sources.get("platform") or "android"
        sessions.append({
            "name": path.relative_to(root).as_posix(),
            "size": stat.st_size,
            "modified_at": stat.st_mtime,
            "session_id": first.get("session_id") or path.stem,
            "started_at": first.get("ts"),
            "elapsed_s": last.get("elapsed_s"),
            "device_serial": last.get("device_serial") or first.get("device_serial"),
            "package": last.get("package") or first.get("package"),
            "status": last.get("status") or first.get("status"),
            "platform": platform,
        })
    sessions.sort(key=lambda item: float(item["modified_at"]), reverse=True)
    return sessions


def _parse_record_output(output: str) -> Dict[str, List[Dict[str, str]]]:
    """Parse ``adb devices -l`` into a list of devices."""
    return {"devices": parse_devices(output)}


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


def _resolve_executable(names):
    """Return the first absolute path among ``names`` that exists on PATH."""
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _detect_adb_state() -> Dict[str, object]:
    """Best-effort snapshot of ``adb`` for the onboarding wizard.

    Returns a dict so the front-end can render the right card without doing
    a second round-trip. The function never raises; day-one failure modes
    (no adb, no device, unauthorized) become explicit fields rather than
    500s, so the wizard always has *something* to show.
    """
    adb_path = _resolve_executable(["adb"])
    if not adb_path:
        return {
            "adb_installed": False,
            "device_state": "missing_tool",
            "device_serial": None,
            "next_step": "尚未找到 adb；請用 Homebrew 裝一次 android-platform-tools",
        }
    try:
        result = subprocess.run(
            ["adb", "devices", "-l"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=4.0,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "adb_installed": True,
            "device_state": "adb_timeout",
            "device_serial": None,
            "next_step": "adb 沒回應，請試著把線拔掉再接上",
        }
    devices = parse_devices(result.stdout or "")
    if not devices:
        return {
            "adb_installed": True,
            "device_state": "disconnected",
            "device_serial": None,
            "next_step": "把手機用線接上 Mac，並打開「USB 偵錯」",
        }
    authorized = [d for d in devices if d.get("state") == "device"]
    if len(authorized) >= 2:
        # Mirror the multi-device guard in collector.select_device: more than
        # one ready device means the recorder cannot pick one for the user.
        # Show the count instead of a "ready" green light so the wizard does
        # not promise a working capture.
        return {
            "adb_installed": True,
            "device_state": "multiple_devices",
            "device_serial": None,
            "next_step": "目前接了 {} 台已授權 Android 裝置，請只留一台（或稍後在 Lab 指定序號）".format(len(authorized)),
        }
    if authorized:
        return {
            "adb_installed": True,
            "device_state": "device",
            "device_serial": authorized[0]["serial"],
            "next_step": "已連線，按「進到 Lab」開始錄製",
        }
    head = devices[0]
    return {
        "adb_installed": True,
        "device_state": head.get("state", "unknown"),
        "device_serial": head.get("serial"),
        "next_step": _android_next_step_for_state(head.get("state", "unknown")),
    }


def _android_next_step_for_state(state: str) -> str:
    if state == "unauthorized":
        return "在手機上按「允許 USB 偵錯」"
    if state == "offline":
        return "把線拔掉重接一次，等手機重新連線"
    if state == "no permissions":
        return "macOS 沒給 adb 存取權限；打開「系統設定 → 隱私與安全性」允許"
    return "請確認手機已開啟 USB 偵錯、並用支援資料的線接上 Mac"


def _detect_ios_state() -> Dict[str, object]:
    """Best-effort snapshot of the iPhone side for the onboarding wizard.

    The iOS capture path is not wired into a recorder yet (only the wizard
    runs against pymobiledevice3). This endpoint only does what is achievable
    without invasive tooling:

    * ``pymobiledevice3_installed`` — does the binary resolve on PATH?
    * ``device_count`` / ``devices`` — how many devices the user can see.
    * ``next_step`` — a one-line human hint; never empty.

    The function returns one of three top-level ``reason`` shapes so the UI
    can show *why* a card is red instead of a generic "plug your phone in":

    * ``pymobiledevice3 未裝`` — tool missing; point the user at ``pipx``.
    * ``無裝置`` — tool present but no iPhone visible; plug-in hint.
    * ``偵測失敗`` — tool crashed, timed out, or printed an error. The
      original stderr / timeout text is preserved in ``note`` so the front-end
      can show the real reason and stop blaming the cable.
    """
    pmd_path = _resolve_executable(["pymobiledevice3"])
    if not pmd_path:
        return {
            "reason": "missing_tool",
            "pymobiledevice3_installed": False,
            "device_count": 0,
            "devices": [],
            "next_step": "終端機跑：brew install pipx && pipx ensurepath（重開 shell），再 pipx install pymobiledevice3",
            "note": "pymobiledevice3 not installed",
        }
    devices, error = _ios_list_devices()
    if error:
        return {
            "reason": "detect_failed",
            "pymobiledevice3_installed": True,
            "device_count": 0,
            "devices": [],
            # Detection failed — never tell the user to "unplug and replug the
            # cable" here. tunneld / Developer Mode / DVT mismatches all land
            # in this branch.
            "next_step": "pymobiledevice3 偵測失敗；若已接線請確認 iOS 17+ 已跑 sudo pymobiledevice3 remote tunneld、並已開「開發者模式」",
            "note": error,
        }
    if not devices:
        return {
            "reason": "no_device",
            "pymobiledevice3_installed": True,
            "device_count": 0,
            "devices": [],
            "next_step": "把 iPhone 用線接上 Mac，並在 iPhone 上按「信任這部電腦」（iOS 17+ 還要跑 sudo pymobiledevice3 remote tunneld）",
        }
    return {
        "reason": "ready",
        "pymobiledevice3_installed": True,
        "device_count": len(devices),
        "devices": devices,
        "next_step": "已偵測到 iPhone，按「進到 Lab」（iPhone 採集尚未整合，僅能預覽）",
    }


def _build_onboarding_state() -> Dict[str, object]:
    android = _detect_adb_state()
    ios = _detect_ios_state()
    return {
        "android": android,
        "ios": ios,
        "ready_to_record": (
            android.get("device_state") == "device"
        ),
        "checked_at": int(time.time()),
    }


def _adb_packages(serial: Optional[str]) -> List[str]:
    """Return the user-visible packages installed on the device."""
    command = ["adb"]
    if serial:
        command.extend(["-s", serial])
    command.extend(["shell", "pm", "list", "packages", "-3"])
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    return sorted(
        line.split(":", 1)[1].strip()
        for line in result.stdout.splitlines()
        if line.startswith("package:")
    )


# Same match order/patterns as ``parse_foreground_package`` in
# ``src/collector.py`` (topResumedActivity on newer AOSP, mResumedActivity
# pre-Android 10, a bare "ResumedActivity" seen on some OEM skins, and
# mCurrentFocus as a last-resort fallback for e.g. dialogs over the game).
# Deliberately duplicated rather than imported: collector.py uses package-
# relative imports (``from .schema import ...``) that only resolve when it's
# loaded as ``src.collector``, while server.py is loaded as a bare top-level
# module by both its CLI entry point and the test suite — cross-importing it
# would require restructuring collector.py's import style, which is off
# limits here (collector's collection logic is untouched by this task).
_FOREGROUND_PATTERNS = (
    r"topResumedActivity\s*=\s*ActivityRecord\{[^\n]*?\bu\d+\s+([\w.$-]+)/",
    r"mResumedActivity\s*:\s*ActivityRecord\{[^\n]*?\bu\d+\s+([\w.$-]+)/",
    r"ResumedActivity\s*:\s*ActivityRecord\{[^\n]*?\bu\d+\s+([\w.$-]+)/",
    r"mCurrentFocus\s*=\s*Window\{[^\n]*?\s([\w.$-]+)/",
)


def _parse_foreground_package(output: str) -> Optional[str]:
    for pattern in _FOREGROUND_PATTERNS:
        match = re.search(pattern, output)
        if match:
            return match.group(1)
    return None


def _adb_foreground_package(serial: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(package, error)`` for the device's current foreground app.

    Read-only ``dumpsys activity activities`` query so the UI can offer a
    "detect foreground app" affordance without the user hunting for the
    package name manually. Exactly one of the tuple members is set.
    """
    command = ["adb"]
    if serial:
        command.extend(["-s", serial])
    command.extend(["shell", "dumpsys", "activity", "activities"])
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5.0,
            check=False,
        )
    except FileNotFoundError:
        return None, "adb not found"
    except subprocess.TimeoutExpired:
        return None, "adb timed out"
    if result.returncode:
        stderr = (result.stderr or "").strip()
        return None, stderr or "adb command failed (device connected?)"
    package = _parse_foreground_package(result.stdout or "")
    if not package:
        return None, "no foreground activity found (device locked or no adb device?)"
    return package, None


# ── iOS helpers (Phase 2) ──────────────────────────────────────────────────
#
# All iOS reads shell out to ``pymobiledevice3`` via PATH lookup so the
# server stays zero-dependency. We never assume a live device — every helper
# returns a (payload, error) tuple so the HTTP handler can answer with a
# 502 carrying a human-readable reason ("pymobiledevice3 not installed",
# "no device", "developer mode off", "tunnel not running", …) instead of
# 500ing on the common "phone isn't plugged in" case the user will hit on
# day one.

def _ios_invoke(args, timeout=5.0):
    """Run ``pymobiledevice3 <args>``; return (stdout, stderr, returncode).

    ``FileNotFoundError`` surfaces as (None, "pymobiledevice3 not installed", None)
    so callers can produce one consistent error string for the UI.
    """
    command = ["pymobiledevice3"] + list(args)
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return None, "pymobiledevice3 not installed (brew install pipx && pipx install pymobiledevice3)", None
    except subprocess.TimeoutExpired:
        return None, "pymobiledevice3 timed out after {:.1f}s".format(timeout), None
    return (result.stdout or "", result.stderr or "", result.returncode)


def _ios_list_devices():
    """Enumerate attached iPhones via ``pymobiledevice3 usbmux list``.

    Returns ``[{"serial": UDID, "state": "..."}, ...]`` — kept the same shape
    as Android's ``adb devices -l`` parser so the UI can reuse the
    ``deviceSelect`` widget across both platforms.

    Newer pymobiledevice3 releases print a JSON array of device objects
    (each carrying ``UniqueDeviceID`` / ``Identifier``, ``DeviceName``,
    ``ProductType``, ...) instead of the older ``UDID : <udid> ConnectionType:
    <type>`` text lines. We try JSON first and fall back to the legacy text
    parser so both CLI vintages work.
    """
    stdout, error, returncode = _ios_invoke(["usbmux", "list"], timeout=3.0)
    if stdout is None:
        # ``_ios_invoke`` only sets ``stdout`` to ``None`` on the
        # FileNotFoundError / TimeoutExpired exception paths, where
        # ``error`` carries a human-readable message. A normal completed
        # process — even one with empty stderr and/or a non-zero exit code —
        # returns ``stdout`` as a string (possibly ``""``), which must fall
        # through to parsing below rather than being mistaken for a failure
        # (stderr being "" is not None, so the old ``error is not None``
        # check here used to bail out on every successful invocation).
        return [], error
    if returncode:
        # Process completed but reported failure (e.g. usbmuxd not running,
        # permission denied) — surface the real reason so
        # ``_detect_ios_state`` can route to its ``detect_failed`` branch
        # instead of silently degrading to "no device", which would tell
        # the user to replug a cable that was never the problem.
        message = (error or "").strip() or "pymobiledevice3 exited with code {}".format(returncode)
        return [], message
    text = stdout or ""
    stripped = text.strip()
    if stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, list):
            devices = []
            for entry in parsed:
                if not isinstance(entry, dict):
                    continue
                serial = entry.get("UniqueDeviceID") or entry.get("Identifier")
                if not serial:
                    continue
                device = {"serial": serial, "state": "device"}
                name = entry.get("DeviceName")
                if name:
                    device["name"] = name
                product = entry.get("ProductType")
                if product:
                    device["product"] = product
                devices.append(device)
            return devices, None
    devices = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("=") or "UDID" not in line:
            continue
        # Lines look like ``UDID : 00008110-... ConnectionType: USB``.
        parts = [segment.strip() for segment in line.split("ConnectionType")]
        head = parts[0]
        if ":" not in head:
            continue
        serial = head.split(":", 1)[1].strip()
        if not serial:
            continue
        devices.append({"serial": serial, "state": "Connected"})
    return devices, None


def _ios_foreground_bundle(serial):
    """Return ``(bundle_id, error)`` for the iPhone's current foreground app.

    pymobiledevice3 exposes DVT's per-process snapshot as
    ``developer dvt sysmon process single`` (NOT ``developer sysmon single``
    — that path silently hits Click's "no such command" exit, which 0-stderr
    parsers mistake for success). The snapshot is a one-shot, table-shaped
    dump of every running process; we do not have a clean foreground bit
    from the public surface, so we degrade to "the first bundle id we see
    in the first data line" — not perfect, but the UI's auto-detect button
    is honest about that.

    On every failure mode we surface a human-readable reason — never a 500
    — so the "自動偵測前景" button can tell the user what to fix (plug
    phone, enable Developer Mode, run ``sudo pymobiledevice3 remote
    tunneld``).
    """
    args = ["developer", "dvt", "sysmon", "process", "single"]
    if serial:
        args.extend(["--udid", serial])
    stdout, error, returncode = _ios_invoke(args, timeout=6.0)
    if stdout is None:
        # Same fix as ``_ios_list_devices``: ``error`` here is really
        # ``_ios_invoke``'s stderr slot, which is ``""`` (not ``None``) on
        # every completed process. The old ``error is not None`` check
        # matched that empty string and bailed out on *every* invocation —
        # including successful ones — before the ``returncode`` branch below
        # (which already has the detailed tunnel/developer-mode/sysmon
        # messaging) ever ran. Only the FileNotFoundError / TimeoutExpired
        # exception paths set ``stdout`` to ``None``.
        return None, error
    if returncode:
        stderr_text = (error or "").strip() or "pymobiledevice3 developer dvt sysmon process single failed"
        lowered = stderr_text.lower()
        # Common day-one failure modes get explicit copy so the user doesn't
        # chase ghosts in pymobiledevice3 docs.
        if "no such command" in lowered and "sysmon" in lowered:
            return None, (
                "pymobiledevice3 CLI shape changed (sysmon moved); "
                "upgrade: pip3 install --upgrade pymobiledevice3"
            )
        if "tunnel" in lowered:
            return None, "tunnel not running (sudo pymobiledevice3 remote tunneld)"
        if "developer" in lowered and "mode" in lowered:
            return None, "Developer Mode is off on the iPhone (Settings → Privacy & Security → Developer Mode)"
        if "developerdiskimage" in lowered or "dvt" in lowered and "not mount" in lowered:
            return None, (
                "Developer Disk Image not mounted; run "
                "`pymobiledevice3 mounter mount` then retry"
            )
        return None, stderr_text
    bundle_id = None
    for raw_line in (stdout or "").splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("pid") or "bundleid" in line.lower():
            continue
        tokens = line.split()
        if len(tokens) >= 3 and "." in tokens[-1] and not tokens[-1].startswith("-"):
            bundle_id = tokens[-1]
            break
    if not bundle_id:
        return None, "no foreground bundle detected (iPhone locked or sysmon empty)"
    return bundle_id, None


class RecordController:
    """Owns the lifecycle of at most one child collector."""

    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir.resolve()
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.process: Optional[subprocess.Popen] = None
        self.output: Optional[Path] = None
        self.session_id: Optional[str] = None
        self.package: Optional[str] = None
        self.serial: Optional[str] = None
        self.interval: float = 1.0
        self.started_at: Optional[float] = None
        self.platform: str = "android"

    def status(self) -> Dict[str, Any]:
        running = self.process is not None and self.process.poll() is None
        payload: Dict[str, Any] = {
            "running": running,
            "session_id": self.session_id,
            "output": self.output.name if self.output else None,
            "package": self.package,
            "serial": self.serial,
            "interval": self.interval,
            "started_at": self.started_at,
            "platform": self.platform,
        }
        if self.process is not None and not running:
            payload["exit_code"] = self.process.returncode
            # A completed session is still reportable until the next start.
        return payload

    def _kill_existing(self) -> None:
        if self.process is None:
            return
        if self.process.poll() is not None:
            self.process = None
            return
        try:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2.0)
        except ProcessLookupError:
            pass
        finally:
            self.process = None

    def start(
        self,
        package: str,
        serial: Optional[str],
        interval: float = 1.0,
        platform: str = "android",
        duration: Optional[float] = None,
    ) -> Tuple[int, Dict[str, Any]]:
        if not package:
            return HTTPStatus.BAD_REQUEST, {"error": "package is required"}
        if interval <= 0:
            return HTTPStatus.BAD_REQUEST, {"error": "interval must be positive"}
        # iPhone capture is not wired in yet. Reject explicitly so the front-end
        # never reaches a half-implemented branch — see the onboarding card
        # copy on the iOS platform for the long-term plan.
        if platform == "ios":
            return HTTPStatus.NOT_IMPLEMENTED, {
                "error": "iPhone recording is not integrated in this release; only the onboarding wizard is wired up"
            }
        if platform != "android":
            return HTTPStatus.BAD_REQUEST, {"error": "platform must be 'android'"}
        if self.process is not None and self.process.poll() is None:
            return HTTPStatus.CONFLICT, {
                "error": "recorder already running",
                "session_id": self.session_id,
                "output": self.output.name if self.output else None,
            }
        # Always sweep a stale child before starting another; we may have
        # inherited a record object whose process already exited but was not
        # awaited.
        self._kill_existing()
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        slug = "".join(
            ch if ch.isalnum() or ch in "._-" else "-" for ch in package
        ).strip("-") or "session"
        session_id = "live-{}-{}".format(stamp, slug)
        output = self.sessions_dir / (session_id + ".jsonl")
        # Refuse to clobber an existing file from a prior crashed run.
        if output.exists():
            return HTTPStatus.CONFLICT, {
                "error": "output already exists: {}".format(output.name),
            }
        command = [
            sys.executable,
            "-m",
            "perf_server.collector",
            "collect",
            package,
            "--interval",
            str(interval),
            "--output",
            str(output),
        ]
        if serial:
            command.extend(["--serial", serial])
        try:
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(PROJECT_ROOT),
            )
        except (FileNotFoundError, OSError) as exc:
            self.process = None
            return HTTPStatus.INTERNAL_SERVER_ERROR, {
                "error": "failed to spawn collector: {}".format(exc)
            }
        self.output = output
        self.session_id = session_id
        self.package = package
        self.serial = serial
        self.interval = interval
        self.started_at = time.time()
        self.platform = platform
        return HTTPStatus.OK, self.status()

    def stop(self) -> Tuple[int, Dict[str, Any]]:
        if self.process is None or self.process.poll() is not None:
            self.process = None
            payload = self.status()
            payload["reason"] = "not running"
            return HTTPStatus.OK, payload
        self._kill_existing()
        payload = self.status()
        payload["reason"] = "stopped"
        return HTTPStatus.OK, payload


def reap_orphan_collectors() -> int:
    """Kill leftover Android collector subprocesses on startup.

    Return the number of processes terminated. Patterns are anchored to the
    project checkout so a collector in another working copy is not killed.
    """
    patterns = (("android_perf.py", "collect"), ("3cdog-perf-collect",))
    pids = set()
    for pattern in patterns:
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern[0]],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        if not result.returncode:
            for raw_pid in result.stdout.split():
                try:
                    pid = int(raw_pid)
                except ValueError:
                    continue
                if pid == os.getpid():
                    continue
                if len(pattern) == 2:
                    try:
                        args = subprocess.run(
                            ["ps", "-p", str(pid), "-o", "args="],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL,
                            text=True,
                            timeout=2.0,
                            check=False,
                        ).stdout
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        args = ""
                    if pattern[1] not in args:
                        continue
                pids.add(pid)
    killed = 0
    for pid in sorted(pids):
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            continue
        except PermissionError:
            continue
    if killed:
        time.sleep(0.5)
        survivors = set()
        for pattern in patterns:
            try:
                result = subprocess.run(
                    ["pgrep", "-f", pattern[0]],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    timeout=2.0,
                    check=False,
                )
            except (FileNotFoundError, subprocess.TimeoutExpired):
                continue
            if not result.returncode:
                for raw_pid in result.stdout.split():
                    try:
                        pid = int(raw_pid)
                    except ValueError:
                        continue
                    if pid != os.getpid():
                        survivors.add(pid)
        for pid in sorted(survivors):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                continue
    return killed


class PerfRequestHandler(BaseHTTPRequestHandler):
    server_version = "AndroidPerf/0.2"

    # These two endpoints are polled on a tight interval by the onboarding
    # wizard / record-status badge, so a healthy 2xx response would otherwise
    # spam the console every second or two. Only successful GETs to exactly
    # these paths are muted — errors and every other path still log.
    _QUIET_GET_PATHS = ("/api/onboarding/state", "/api/record/status")

    @property
    def sessions_dir(self) -> Path:
        return self.server.sessions_dir  # type: ignore[attr-defined]

    @property
    def recorder(self) -> RecordController:
        return self.server.recorder  # type: ignore[attr-defined]

    def log_request(self, code="-", size="-") -> None:  # noqa: N802 - stdlib hook
        if isinstance(code, HTTPStatus):
            code_value = code.value
        else:
            code_value = code
        try:
            status = int(code_value)
        except (TypeError, ValueError):
            status = None
        path = urlparse(self.path).path if getattr(self, "path", None) else ""
        if (
            self.command == "GET"
            and path in self._QUIET_GET_PATHS
            and status is not None
            and 200 <= status < 300
        ):
            return
        super().log_request(code, size)

    def log_message(self, fmt: str, *args: object) -> None:
        print("[android-perf] {} - {}".format(self.address_string(), fmt % args))

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _serve_static(self, request_path: str) -> None:
        relative = "index.html" if request_path in ("", "/") else unquote(request_path.lstrip("/"))
        root = WEB_DIR.resolve()
        candidate = (root / relative).resolve()
        if not _is_within(candidate, root) or not candidate.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:")
        self.end_headers()
        self.wfile.write(body)

    def _session_from_query(self, query: Dict[str, List[str]]) -> Path:
        name = query.get("name", [""])[0]
        path = resolve_session(self.sessions_dir, name)
        if not path.is_file():
            raise FileNotFoundError(name)
        return path

    def _serve_session(self, query: Dict[str, List[str]]) -> None:
        try:
            path = self._session_from_query(query)
            offset = int(query.get("offset", ["0"])[0])
            limit = int(query.get("limit", ["1000"])[0])
            chunk = read_session_chunk(path, offset, limit)
        except FileNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, "session not found")
            return
        except (ValueError, OSError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        chunk["name"] = path.relative_to(self.sessions_dir.resolve()).as_posix()
        self._send_json(HTTPStatus.OK, chunk)

    def _serve_status(self, query: Dict[str, List[str]]) -> None:
        try:
            if query.get("name"):
                path = self._session_from_query(query)
            else:
                sessions = list_sessions(self.sessions_dir)
                if not sessions:
                    self._send_json(HTTPStatus.OK, {"connected": False, "status": "no_session", "reason": "尚無 session 資料"})
                    return
                path = resolve_session(self.sessions_dir, str(sessions[0]["name"]))
            first, last = _first_and_last(path)
            record = last or first or {}
            state = record.get("status") or "unknown"
            errors = record.get("errors") if isinstance(record.get("errors"), list) else []
            self._send_json(HTTPStatus.OK, {
                "connected": state not in ("device_error", "disconnected"),
                "status": state,
                "reason": errors[-1] if errors else None,
                "session": record.get("session_id") or path.stem,
                "name": path.relative_to(self.sessions_dir.resolve()).as_posix(),
                "device": record.get("device_serial"),
                "package": record.get("package"),
                "elapsed_s": record.get("elapsed_s"),
                "updated_at": record.get("ts"),
            })
        except FileNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, "session not found")
        except (ValueError, OSError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 64 * 1024))
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("body is not JSON")
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        return payload

    def _serve_record_start(self) -> None:
        try:
            body = self._read_json_body()
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        package = body.get("package") or ""
        serial = body.get("serial")
        if serial in ("", None):
            serial = None
        try:
            interval = float(body.get("interval", 1.0))
        except (TypeError, ValueError):
            self._error(HTTPStatus.BAD_REQUEST, "interval must be a number")
            return
        # ``duration`` is accepted on the wire for forward compatibility with a
        # future iPhone capture, but it has no effect on the Android collector
        # today. We accept it as an optional body field so legacy clients
        # (the simplified test driver, the FPS Lab UI) keep behaving the way
        # they did before iOS recording was split out of this release.
        duration_field = body.get("duration")
        duration_value: Optional[float]
        if duration_field in (None, ""):
            duration_value = None
        else:
            try:
                duration_value = float(duration_field)
            except (TypeError, ValueError):
                self._error(HTTPStatus.BAD_REQUEST, "duration must be a number")
                return
        # Default to "android" so legacy clients (and the simplified test
        # driver that POSTs only package + serial) keep working. The 08-21
        # brief introduced "ios" as a second supported value, but iPhone
        # recording is not integrated yet — see the wizard card and the
        # ``RecordController.start`` iOS rejection for the long-term plan.
        platform = body.get("platform") or "android"
        status, payload = self.recorder.start(package, serial, interval, platform=platform, duration=duration_value)
        self._send_json(status, payload)

    def _serve_record_stop(self) -> None:
        status, payload = self.recorder.stop()
        self._send_json(status, payload)

    def _serve_record_status(self) -> None:
        self._send_json(HTTPStatus.OK, self.recorder.status())

    def _serve_devices(self) -> None:
        command = ["adb", "devices", "-l"]
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=4.0,
                check=False,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "adb unavailable: {}".format(exc))
            return
        devices = parse_devices(result.stdout or "")
        self._send_json(HTTPStatus.OK, {"devices": devices})

    def _serve_packages(self, query: Dict[str, List[str]]) -> None:
        serial = query.get("serial", [None])[0]
        packages = _adb_packages(serial)
        self._send_json(HTTPStatus.OK, {"packages": packages})

    def _serve_foreground(self, query: Dict[str, List[str]]) -> None:
        serial = query.get("serial", [None])[0]
        package, error = _adb_foreground_package(serial)
        if error:
            self._error(HTTPStatus.BAD_GATEWAY, error)
            return
        self._send_json(HTTPStatus.OK, {"package": package})

    def _serve_ios_bundle_name(self, query: Dict[str, List[str]]) -> None:
        bundle = (query.get("bundle", [""])[0] or "").strip()
        if not bundle:
            self._error(HTTPStatus.BAD_REQUEST, "bundle query param required")
            return
        # ``friendly`` is the human-readable label (mapped name when the
        # bundle id is in our small lookup table, otherwise the raw id).
        # The front-end's ``friendlyBundleName()`` mirrors this same
        # fallback. Returning the raw id here lets the UI surface the
        # unmapped bundle id without a second round-trip.
        friendly = friendly_name(bundle)
        self._send_json(HTTPStatus.OK, {
            "bundle": bundle,
            "name": friendly,
            "friendly": friendly,
        })

    def _serve_onboarding_state(self) -> None:
        """Return the wizard's two-platform snapshot for the landing page.

        Always returns 200 with a populated body — even on tool-missing
        outcomes — so the front-end never has to render an empty spinner.
        """
        self._send_json(HTTPStatus.OK, _build_onboarding_state())

    def _serve_ios_devices(self) -> None:
        # The iOS list-devices path is best-effort: when pymobiledevice3 is
        # missing or the device is unplugged, the UI still needs an empty
        # list so the "iPhone 來源" placeholder can render cleanly. The
        # failure reason is surfaced in ``note`` so the user can fix it
        # without a separate API call.
        devices, error = _ios_list_devices()
        payload: Dict[str, Any] = {"devices": devices}
        if error:
            payload["note"] = error
        self._send_json(HTTPStatus.OK, payload)

    def _serve_ios_foreground(self, query: Dict[str, List[str]]) -> None:
        serial = query.get("serial", [None])[0]
        bundle, error = _ios_foreground_bundle(serial)
        if error:
            self._error(HTTPStatus.BAD_GATEWAY, error)
            return
        self._send_json(HTTPStatus.OK, {
            "bundle": bundle,
            "name": friendly_name(bundle),
        })

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path == "/api/sessions":
            self._send_json(HTTPStatus.OK, {"sessions": list_sessions(self.sessions_dir)})
        elif parsed.path == "/api/session":
            self._serve_session(query)
        elif parsed.path == "/api/status":
            self._serve_status(query)
        elif parsed.path == "/api/devices":
            self._serve_devices()
        elif parsed.path == "/api/packages":
            self._serve_packages(query)
        elif parsed.path == "/api/foreground":
            self._serve_foreground(query)
        elif parsed.path == "/api/ios/bundle_name":
            self._serve_ios_bundle_name(query)
        elif parsed.path == "/api/ios/devices":
            self._serve_ios_devices()
        elif parsed.path == "/api/ios/foreground":
            self._serve_ios_foreground(query)
        elif parsed.path == "/api/onboarding/state":
            self._serve_onboarding_state()
        elif parsed.path == "/api/record/status":
            self._serve_record_status()
        elif parsed.path.startswith("/api/"):
            self._error(HTTPStatus.NOT_FOUND, "unknown API endpoint")
        else:
            self._serve_static(parsed.path)

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook
        parsed = urlparse(self.path)
        if parsed.path == "/api/record/start":
            self._serve_record_start()
        elif parsed.path == "/api/record/stop":
            self._serve_record_stop()
        else:
            self._error(HTTPStatus.NOT_FOUND, "unknown API endpoint")


class PerfHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: Tuple[str, int], sessions_dir: Path):
        self.sessions_dir = sessions_dir.resolve()
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.recorder = RecordController(self.sessions_dir)
        super().__init__(address, PerfRequestHandler)

    def server_close(self) -> None:  # type: ignore[override]
        try:
            self.recorder._kill_existing()
        finally:
            super().server_close()


def make_server(host: str, port: int, sessions_dir: Path) -> PerfHTTPServer:
    return PerfHTTPServer((host, port), sessions_dir)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve Android Perf live and recorded reports")
    parser.add_argument("--host", default="127.0.0.1", help="listen host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="listen port (default: 8765)")
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        default=Path.home() / ".3cdog-perf" / "sessions",
        help="directory containing session .jsonl files (default: ~/.3cdog-perf/sessions)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    orphans = reap_orphan_collectors()
    if orphans:
        print("reaped {} orphan collector process(es)".format(orphans), file=sys.stderr)
    server = make_server(args.host, args.port, args.sessions_dir)
    host, port = server.server_address[:2]
    print("3cdog Perf UI: http://{}:{}".format(host, port))
    print("Sessions: {}".format(server.sessions_dir))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()