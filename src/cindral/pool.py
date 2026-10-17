"""Pool and queue snapshots for the control panel and metrics endpoint."""
import json
import sqlite3
import time
from collections.abc import Iterable
from typing import Any

from .agent import ONLINE_WINDOW_SECONDS as AGENT_ONLINE_SECONDS
from .build_info import build_labels, current_build
from .stalls import (
    DEFERRED,
    IDLE_DEVICES,
    NO_AGENTS,
    NO_ELIGIBLE_DEVICE,
    STALL_REASONS,
    _meta_total,
    _oldest_pending_age,
    _deferred,
    _queue_depth,
    _unclaimable,
)
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
from .lease_reclaim import prune_agents, spent_lease

__all__ = ["pool_snapshot", "render_metrics", "store_metrics", "store_snapshot"]

STALL_SECONDS = 300
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


def _agents(connection: sqlite3.Connection, snapshot_time: float) -> dict[str, dict[str, Any]]:
    # prune before selecting: presence past retention is retired, and a pool
    # whose agents all went offline never takes the write path again, so the
    # snapshot is where the cleanup has to happen
    prune_agents(connection, snapshot_time)
    rows = connection.execute(
        "SELECT device, labels, max_concurrency, last_seen, build FROM agents"
    ).fetchall()
    return {
        row["device"]: {
            "labels": tuple(json.loads(row["labels"])),
            "max_concurrency": int(row["max_concurrency"]),
            "last_seen": float(row["last_seen"]),
            "build": _read_build(row["build"]),
        }
        for row in rows
    }


def _read_build(raw: object) -> dict[str, str]:
    """The build a device reported, or an empty one when it reported nothing.

    Absent rather than assumed: a device on an older agent does not send a
    build, and that is a fact about the device rather than a parse failure.
    """
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {key: value for key, value in parsed.items() if isinstance(key, str) and isinstance(value, str)}


def _device(
    name: str,
    runner: Any,
    leases: list[dict[str, Any]],
    agent: dict[str, Any] | None = None,
    snapshot_time: float = 0.0,
) -> dict[str, Any]:
    """Build one device row from configured state, its leases, and its agent.

    An agent's own claim on how busy it is is never read here: active work comes
    from the leases the broker granted, so a device cannot report free capacity
    it is already spending. Free slots are offered only while the agent is
    reachable, because a device that stopped reporting takes no new work; the
    limit it was last configured with is still reported. Runner status stays the
    configured value, because whether GitHub sees the host is a separate question
    from whether an agent is talking to the broker.
    """
    online = agent is not None and snapshot_time - agent["last_seen"] <= AGENT_ONLINE_SECONDS
    max_concurrency = agent["max_concurrency"] if agent is not None else None
    active_jobs = len(leases)
    # a device absent from state.json is known only by its agent, so the labels
    # it advertises are all the panel has to show
    labels = agent["labels"] if runner is None and agent else getattr(runner, "labels", ())
    return {
        "name": name,
        "status": getattr(runner, "status", "online"),
        "healthy": getattr(runner, "healthy", True),
        "busy": bool(getattr(runner, "busy", False)) or bool(leases),
        "labels": list(labels),
        "current_lease": leases[0] if leases else None,
        "leases": leases,
        "agent_online": online,
        # what the device says it is running. A device agent is a checkout on
        # the host, so this is the only place its age is visible: a device behind
        # the broker claims a job and answers with code that predates it
        "build": agent["build"] if agent is not None else {},
        "build_known": agent is not None and bool(agent["build"]),
        "last_seen": agent["last_seen"] if agent is not None else None,
        "max_concurrency": max_concurrency,
        "active_jobs": active_jobs,
        "available_slots": (
            None if not online or max_concurrency is None else max(0, max_concurrency - active_jobs)
        ),
    }


def _devices(
    runners: Iterable[Any],
    leases: dict[str, list[dict[str, Any]]],
    agents: dict[str, dict[str, Any]],
    snapshot_time: float,
) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for runner in runners:
        seen.add(runner.name)
        devices.append(
            _device(runner.name, runner, leases.get(runner.name, []), agents.get(runner.name), snapshot_time)
        )
    # a device that only ever appeared as an agent or a lease is absent from
    # state.json, and hiding it would make a working machine look nonexistent.
    # A name a configured runner already claimed is skipped: a second row would
    # report that runner as online and healthy whatever state.json says, and
    # would count its leases and slots twice.
    for name in sorted((set(leases) | set(agents)) - seen):
        devices.append(
            _device(name, None, leases.get(name, []), agents.get(name), snapshot_time)
        )
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
        # a snapshot is polled, so it carries step names and outcomes but not
        # their output: 50 recent jobs at full step size would be tens of
        # megabytes for one poll. The job page serves the output.
        for step in job.get("steps", []):
            step.pop("output", None)
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
    counts, reclaimable = _queue_depth(connection, snapshot_time)
    agents = _agents(connection, snapshot_time)
    devices = _devices(runners, _active_leases(connection, snapshot_time), agents, snapshot_time)
    pending_age = round(_oldest_pending_age(connection, snapshot_time), 2)
    stalled_reason = None
    if counts[PENDING] and pending_age >= STALL_SECONDS:
        if not any(device["agent_online"] for device in devices):
            stalled_reason = NO_AGENTS
        elif not any((device["available_slots"] or 0) > 0 for device in devices):
            # capacity, not a device sitting idle: the string is what the panel,
            # the metrics label and the docs already read, so the condition
            # moved rather than the name
            stalled_reason = IDLE_DEVICES
        elif _deferred(connection, snapshot_time):
            stalled_reason = DEFERRED
        elif _unclaimable(connection, agents, snapshot_time):
            stalled_reason = NO_ELIGIBLE_DEVICE
    return {
        "devices": devices,
        "queue_depth": counts,
        "expired_lease_count": reclaimable,
        "oldest_pending_age_seconds": pending_age,
        "stalled_reason": stalled_reason,
        "reclaim_count": _meta_total(connection, "reclaims"),
        "live_device_reclaim_count": _meta_total(connection, "reclaims_live"),
        "success_count": counts.get(SUCCESS, 0),
        "failure_count": counts.get(FAILURE, 0),
        "recent_jobs": _recent_jobs(connection) if include_recent else [],
        # the panel reads this to name the build it is showing, so the version
        # travels with the snapshot every page already fetches
        "build": current_build().as_dict(),
    }


def render_metrics(runners: Iterable[Any], snapshot: dict[str, Any] | None) -> str:
    """Render Prometheus text for the pool and queue."""
    # materialize once: the counts and the snapshot both walk this sequence
    devices = list(runners)
    online = sum(1 for device in devices if getattr(device, "status", None) == "online")

    lines = [
        "# HELP cindral_build Build metadata for the running broker.",
        "# TYPE cindral_build gauge",
        f"cindral_build{{{build_labels()}}} 1",
        "# HELP cindral_devices Total configured runner devices by status.",
        "# TYPE cindral_devices gauge",
        f'cindral_devices{{status="online"}} {online}',
        f'cindral_devices{{status="offline"}} {len(devices) - online}',
        "",
    ]

    if snapshot is None:
        return "\n".join(lines) + "\n"

    queue = snapshot["queue_depth"]
    devices_in_pool = snapshot["devices"]
    busy = sum(1 for device in devices_in_pool if device["busy"])
    connected = sum(1 for device in devices_in_pool if device["agent_online"])
    free_slots = sum(device["available_slots"] or 0 for device in devices_in_pool)
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
        "# HELP cindral_pool_stalled Why a pending job older than five minutes has not moved.",
        "# TYPE cindral_pool_stalled gauge",
        *(
            f'cindral_pool_stalled{{reason="{reason}"}} {int(snapshot["stalled_reason"] == reason)}'
            for reason in STALL_REASONS
        ),
        "",
        "# HELP cindral_expired_leases Running jobs whose lease the reclaim can take back.",
        "# TYPE cindral_expired_leases gauge",
        f'cindral_expired_leases {snapshot["expired_lease_count"]}',
        "",
        "# HELP cindral_reclaims_total Total number of expired leases reclaimed.",
        "# TYPE cindral_reclaims_total counter",
        f'cindral_reclaims_total {snapshot["reclaim_count"]}',
        "",
        "# HELP cindral_reclaims_live_device_total Reclaims taken from a device that was still connected.",
        "# TYPE cindral_reclaims_live_device_total counter",
        f'cindral_reclaims_live_device_total {snapshot["live_device_reclaim_count"]}',
        "",
        "# HELP cindral_devices_busy Number of devices currently executing a lease.",
        "# TYPE cindral_devices_busy gauge",
        f'cindral_devices_busy {busy}',
        "",
        # configured and connected are counted apart on purpose: a device in
        # state.json with no agent is the usual cause of a stalled queue
        "# HELP cindral_agent_devices Pool devices by whether a direct agent is connected.",
        "# TYPE cindral_agent_devices gauge",
        f'cindral_agent_devices{{connected="true"}} {connected}',
        f'cindral_agent_devices{{connected="false"}} {len(devices_in_pool) - connected}',
        "",
        "# HELP cindral_agent_available_slots Free slots on pool devices with a connected agent.",
        "# TYPE cindral_agent_available_slots gauge",
        f"cindral_agent_available_slots {free_slots}",
        "",
    ]
    return "\n".join(lines) + "\n"


def store_snapshot(
    store: Any,
    runners: Iterable[Any] = (),
    now: float | None = None,
    include_recent: bool = True,
) -> dict[str, Any]:
    """Read a pool snapshot through a job store, which owns the connection."""
    return store._read(
        lambda connection: pool_snapshot(connection, runners, now, include_recent)
    )


def store_metrics(store: Any, runners: Iterable[Any] = ()) -> str:
    """Render Prometheus text without materializing the recent-job log."""
    # the snapshot and the render each walk this iterable, so a one-shot
    # sequence would be exhausted before the gauges are counted
    devices = list(runners)
    return store._read(
        lambda connection: render_metrics(devices, pool_snapshot(connection, devices, include_recent=False))
    )
