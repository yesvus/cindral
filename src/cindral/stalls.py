"""Why a pool that is not working is not working.

A queue that is moving slowly and a queue that is stuck look the same from the
outside, and the difference is the only thing worth an operator's attention. These
name the reason and nothing else: the reading is a statement about capacity, so
each one has to be able to say it is not a stall rather than default to one.

Every condition here is evidence before it is a conclusion. Capacity the pool has
and work it can take is a queue moving slowly, and calling it a stall is what
sends an operator to buy runners the queue never needed.
"""
import json
import sqlite3
from typing import Any

from .agent import ONLINE_WINDOW_SECONDS as AGENT_ONLINE_SECONDS
from .jobs import FAILURE, PENDING, RUNNING, SUCCESS
from .lease_reclaim import spent_lease

NO_AGENTS = "no_agents"
IDLE_DEVICES = "idle_devices"
DEFERRED = "deferred"
NO_ELIGIBLE_DEVICE = "no_eligible_device"
STALL_REASONS = (NO_AGENTS, IDLE_DEVICES, DEFERRED, NO_ELIGIBLE_DEVICE)


def _queue_depth(connection: sqlite3.Connection, snapshot_time: float) -> tuple[dict[str, int], int]:
    """Count jobs per state, treating a spent lease as awaiting reclaim.

    A running row the reclaim can take back is not occupying a device, so
    counting it as running would report a job on a device the panel shows idle.
    A lease that lapsed under a device that is still announcing is the other
    case: the device is on the run, so the run stays running until the grace
    decides, and the count says how many the reclaim would actually take.
    """
    counts = {PENDING: 0, RUNNING: 0, SUCCESS: 0, FAILURE: 0}
    for row in connection.execute("SELECT status, count(*) AS count FROM jobs GROUP BY status"):
        counts[row["status"]] = int(row["count"])

    spent, bounds = spent_lease(snapshot_time)
    reclaimable = connection.execute(
        f"SELECT count(*) AS count FROM jobs WHERE status = ? AND {spent}", (RUNNING, *bounds)
    ).fetchone()
    reclaimable_count = int(reclaimable["count"]) if reclaimable is not None else 0
    counts[RUNNING] -= reclaimable_count
    counts[PENDING] += reclaimable_count
    return counts, reclaimable_count


def _waiting_rows(snapshot_time: float) -> tuple[str, tuple[float, ...]]:
    """The condition and values that mark a run as waiting, for every reader here.

    `queue_depth` counts a spent lease as waiting, so the age, the backoff check
    and the label check have to count the same rows. A spent lease is still
    `running` in the table, which is how one snapshot came to report a job
    waiting, zero seconds old, and no stall.
    """
    spent, bounds = spent_lease(snapshot_time)
    return f"(status = ? OR (status = ? AND {spent}))", (PENDING, RUNNING, *bounds)


def _deferred(connection: sqlite3.Connection, snapshot_time: float) -> bool:
    """Whether no claimable run is waiting, so the queue is inside a backoff.

    A lost run goes pending immediately and is claimable once its backoff ends,
    which is a device problem and a stall neither. Reporting it as one is what
    sent an operator looking for capacity the pool already had.
    """
    waiting, params = _waiting_rows(snapshot_time)
    row = connection.execute(
        f"SELECT min(COALESCE(available_at, 0)) AS soonest FROM jobs WHERE {waiting}", params
    ).fetchone()
    return row is not None and row["soonest"] is not None and row["soonest"] > snapshot_time


def _unclaimable(
    connection: sqlite3.Connection, agents: dict[str, dict[str, Any]], snapshot_time: float
) -> bool:
    """Whether a claimable run needs labels no connected device advertises.

    The queue holds both sides of that match, so a run no device can take is a
    diagnosis rather than a mystery. The per-repository run cap is not stored, so
    a run held by that reports no stall at all instead of blaming a device.
    """
    waiting, params = _waiting_rows(snapshot_time)
    claimable = connection.execute(
        f"SELECT labels FROM jobs WHERE {waiting}"
        " AND (available_at IS NULL OR available_at <= ?)",
        (*params, snapshot_time),
    ).fetchall()
    offered = [
        set(agent["labels"])
        for agent in agents.values()
        if snapshot_time - agent["last_seen"] <= AGENT_ONLINE_SECONDS
    ]
    return not any(
        set(json.loads(row["labels"])).issubset(labels) for row in claimable for labels in offered
    )


def _oldest_pending_age(connection: sqlite3.Connection, snapshot_time: float) -> float:
    waiting, params = _waiting_rows(snapshot_time)
    row = connection.execute(
        f"SELECT min(created_at) AS oldest FROM jobs WHERE {waiting}", params
    ).fetchone()
    if row is None or row["oldest"] is None:
        return 0.0
    return max(0.0, snapshot_time - float(row["oldest"]))


def _meta_total(connection: sqlite3.Connection, key: str) -> int:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return int(row["value"]) if row is not None else 0


