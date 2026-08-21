"""Stable JSONL record schema for android-perf sessions."""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = 1

RECORD_FIELDS = (
    "schema_version",
    "session_id",
    "ts",
    "elapsed_s",
    "package",
    "device_serial",
    "status",
    "fps",
    "jank",
    "jank_pct",
    "frame_times_ms",
    "cpu_total",
    "cpu_per_core",
    "gpu",
    "mem_pss_mb",
    "battery_power_w",
    "temp_c",
    "sources",
    "errors",
)

METRIC_FIELDS = (
    "fps",
    "jank",
    "jank_pct",
    "frame_times_ms",
    "cpu_total",
    "cpu_per_core",
    "gpu",
    "mem_pss_mb",
    "battery_power_w",
    "temp_c",
)

# Fields that may be omitted on legacy rows. The collector only fills these
# when its corresponding source produced a usable sample.
OPTIONAL_RECORD_FIELDS = ("frame_times_ms",)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_record(
    session_id: str,
    elapsed_s: float,
    package: str,
    device_serial: str,
    status: str = "ok",
    ts: Optional[str] = None,
    sources: Optional[Dict[str, Optional[str]]] = None,
    errors: Optional[List[str]] = None,
    **metrics: Any
) -> Dict[str, Any]:
    """Build a record with all canonical fields present and in the canonical order."""
    record: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "ts": ts or utc_timestamp(),
        "elapsed_s": round(float(elapsed_s), 3),
        "package": package,
        "device_serial": device_serial,
        "status": status,
    }
    for field in METRIC_FIELDS:
        record[field] = metrics.get(field)
    # Strip optional fields that the caller did not provide so legacy readers
    # that key-set-compare against the canonical tuple still succeed once new
    # optional fields are added.
    for field in OPTIONAL_RECORD_FIELDS:
        if field not in metrics:
            record.pop(field, None)
    record["sources"] = sources or {}
    record["errors"] = errors or []
    return record


def validate_record(record: Dict[str, Any]) -> None:
    """Raise ValueError when a record drifts from the public schema.

    Optional fields such as ``frame_times_ms`` may be absent on legacy rows;
    we only require the canonical positional fields.
    """
    canonical = [field for field in RECORD_FIELDS if field not in OPTIONAL_RECORD_FIELDS]
    actual = [field for field in record if field not in OPTIONAL_RECORD_FIELDS]
    if actual != canonical:
        raise ValueError("record fields differ from schema: %r" % (actual,))
    if record["status"] not in (
        "ok",
        "app_background",
        "device_error",
        "device_unauthorized",
        "device_offline",
        "device_disconnected",
        "adb_timeout",
        "collector_error",
    ):
        raise ValueError("invalid status: %s" % record["status"])
    if not isinstance(record["errors"], list):
        raise ValueError("errors must be a list")
    if not isinstance(record["sources"], dict):
        raise ValueError("sources must be an object")
    frame_times = record.get("frame_times_ms")
    if frame_times is not None and not isinstance(frame_times, list):
        raise ValueError("frame_times_ms must be a list of floats when present")
