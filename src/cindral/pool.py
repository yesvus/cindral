"""Pool and queue snapshots for the control panel and metrics endpoint."""
import sqlite3
import time
from collections.abc import Iterable
from typing import Any

from .jobs import (
    FAILURE,
    PENDING,
    RUNNING,
    SUCCESS,
    Job,
    RECENT_JOB_LIMIT,
    SNAPSHOT_LOG_CHARS,
    _row,
)

__all__ = ["pool_snapshot", "render_metrics"]


def _queue_depth(connection: sqlite3.Connection, snapshot_time: float) -> tuple[dict[str, int], int]:
    """Count jobs per state, treating an expired lease as awaiting reclaim.

    A running row whose lease already elapsed is not occupying a device, so
    counting it as running would report a job on a device the panel shows idle.
    """
    counts = {PENDING: 0, RUNNING: 0, SUCCESS: 0, FAILURE: 0}
    for row in connection.execute("SELECT status, count(*) AS count FROM jobs GROUP BY status"):
        counts[row["status"]] = int(row["count"])

    expired = connection.execute(
        "SELECT count(*) AS count FROM jobs"
        " WHERE status = ? AND lease_expires IS NOT NULL AND lease_expires <= ?",
        (RUNNING, snapshot_time),
    ).fetchone()
    expired_count = int(expired["count"]) if expired is not None else 0
    counts[RUNNING] -= expired_count
    counts[PENDING] += expired_count
    return counts, expired_count


def _oldest_pending_age(connection: sqlite3.Connection, snapshot_time: float) -> float:
    row = connection.execute(
        "SELECT min(created_at) AS oldest FROM jobs WHERE status = ?",
        (PENDING,),
    ).fetchone()
    if row is None or row["oldest"] is None:
        return 0.0
    return max(0.0, snapshot_time - float(row["oldest"]))


def _reclaim_total(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT value FROM meta WHERE key = 'reclaims'").fetchone()
    return int(row["value"]) if row is not None else 0


def _active_leases(connection: sqlite3.Connection, snapshot_time: float) -> dict[str, list[dict[str, Any]]]:
    rows = connection.execute(
        "SELECT id, repository, sha, ref, device, lease_expires FROM jobs"
        " WHERE status = ? AND lease_expires IS NOT NULL AND lease_expires > ?",
        (RUNNING, snapshot_time),
    ).fetchall()

    leases: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        device = row["device"]
        if not device:
            continue
        expires = float(row["lease_expires"])
        leases.setdefault(device, []).append(
            {
                "job_id": row["id"],
                "repository": row["repository"],
                "sha": row["sha"],
                "ref": row["ref"],
                "lease_expires": expires,
                "remaining_seconds": max(0.0, expires - snapshot_time),
            }
        )
    # a device should hold one lease, so the soonest expiry is the one that
    # frees the slot first; extras stay listed so a double claim is visible
    # instead of silently dropped
    for device_leases in leases.values():
        device_leases.sort(key=lambda lease: lease["lease_expires"])
    return leases


def _device(name: str, runner: Any, leases: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "name": name,
        "status": getattr(runner, "status", "online"),
        "healthy": getattr(runner, "healthy", True),
        "busy": bool(getattr(runner, "busy", False)) or bool(leases),
        "labels": list(getattr(runner, "labels", ())),
        "current_lease": leases[0] if leases else None,
        "leases": leases,
    }


def _devices(
    runners: Iterable[Any],
    leases: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for runner in runners:
        seen.add(runner.name)
        devices.append(_device(runner.name, runner, leases.get(runner.name, [])))
    for name, device_leases in leases.items():
        if name not in seen:
            devices.append(_device(name, None, device_leases))
    return devices


def _recent_jobs(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute(
        "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?",
        (RECENT_JOB_LIMIT,),
    ).fetchall()
    recent: list[dict[str, Any]] = []
    for row in rows:
        job = _row(row).as_dict()
        # the panel renders a preview, not the full stored log
        if len(job["log"]) > SNAPSHOT_LOG_CHARS:
            job["log"] = job["log"][-SNAPSHOT_LOG_CHARS:]
            job["log_truncated"] = True
        recent.append(job)
    return recent


def pool_snapshot(
    connection: sqlite3.Connection,
    runners: Iterable[Any] = (),
    now: float | None = None,
    include_recent: bool = True,
) -> dict[str, Any]:
    """Build a pool snapshot from an open jobs connection."""
    snapshot_time = time.time() if now is None else now
    counts, expired_leases = _queue_depth(connection, snapshot_time)
    return {
        "devices": _devices(runners, _active_leases(connection, snapshot_time)),
        "queue_depth": counts,
        "expired_lease_count": expired_leases,
        "oldest_pending_age_seconds": round(
            _oldest_pending_age(connection, snapshot_time), 2
        ),
        "reclaim_count": _reclaim_total(connection),
        "success_count": counts.get(SUCCESS, 0),
        "failure_count": counts.get(FAILURE, 0),
        "recent_jobs": _recent_jobs(connection) if include_recent else [],
    }


def render_metrics(runners: Iterable[Any], snapshot: dict[str, Any] | None) -> str:
    """Render Prometheus text for the pool and queue."""
    # materialize once: the counts and the snapshot both walk this sequence
    devices = list(runners)
    online = sum(1 for device in devices if getattr(device, "status", None) == "online")

    lines = [
        "# HELP cindral_devices Total configured runner devices by status.",
        "# TYPE cindral_devices gauge",
        f'cindral_devices{{status="online"}} {online}',
        f'cindral_devices{{status="offline"}} {len(devices) - online}',
        "",
    ]

    if snapshot is None:
        return "\n".join(lines) + "\n"

    queue = snapshot["queue_depth"]
    busy = sum(1 for device in snapshot["devices"] if device["busy"])
    lines += [
        "# HELP cindral_queue_depth Number of jobs in the queue by state.",
        "# TYPE cindral_queue_depth gauge",
        f'cindral_queue_depth{{state="pending"}} {queue.get(PENDING, 0)}',
        f'cindral_queue_depth{{state="running"}} {queue.get(RUNNING, 0)}',
        f'cindral_queue_depth{{state="success"}} {queue.get(SUCCESS, 0)}',
        f'cindral_queue_depth{{state="failure"}} {queue.get(FAILURE, 0)}',
        "",
        "# HELP cindral_oldest_pending_seconds Age in seconds of the oldest pending job.",
        "# TYPE cindral_oldest_pending_seconds gauge",
        f'cindral_oldest_pending_seconds {snapshot["oldest_pending_age_seconds"]}',
        "",
        "# HELP cindral_expired_leases Running jobs whose lease already expired.",
        "# TYPE cindral_expired_leases gauge",
        f'cindral_expired_leases {snapshot["expired_lease_count"]}',
        "",
        "# HELP cindral_reclaims_total Total number of expired leases reclaimed.",
        "# TYPE cindral_reclaims_total counter",
        f'cindral_reclaims_total {snapshot["reclaim_count"]}',
        "",
        "# HELP cindral_devices_busy Number of devices currently executing a lease.",
        "# TYPE cindral_devices_busy gauge",
        f'cindral_devices_busy {busy}',
        "",
    ]
    return "\n".join(lines) + "\n"
