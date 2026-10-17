"""Device presence, which is a table of its own and a question of its own.

Separate from the job queue because nothing about it is a job: a device announces
itself, the pool reads what it announced, and a device that stops announcing is
retired rather than failed. Keeping it apart also keeps the columns it grew, the
build it reports, from being a migration on the queue.
"""
import json
import sqlite3
import time

from .lease_reclaim import prune_agents


def record_heartbeat(
    connection: sqlite3.Connection,
    device: str,
    labels: tuple[str, ...] | list[str],
    max_concurrency: int,
    now: float | None = None,
    build: dict[str, str] | None = None,
) -> float:
    """Record that a device is there, and what it is running.

    The build goes in with it, because a device agent runs from a checkout
    rather than an image and there is no other way for the broker to learn that a
    device is behind the code it is being asked to run.
    """
    seen_at = time.time() if now is None else now
    prune_agents(connection, seen_at)
    connection.execute(
        "INSERT OR REPLACE INTO agents (device, labels, max_concurrency, last_seen, build)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            device,
            json.dumps(list(labels)),
            int(max_concurrency),
            seen_at,
            json.dumps(build or {}),
        ),
    )
    return seen_at


def read_agents(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT device, labels, max_concurrency, last_seen, build FROM agents"
    ).fetchall()
