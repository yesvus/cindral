"""Select an eligible pending job without letting one repository fill the pool."""
import json
import sqlite3


def positive_repository_limit(value: str) -> int:
    limit = int(value)
    if limit <= 0:
        raise ValueError("CINDRAL_MAX_REPOSITORY_RUNNING must be positive")
    return limit


def pick_job(
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
    labels: set[str],
    max_repository_running: int | None,
) -> sqlite3.Row | None:
    active = {
        row["repository"]: int(row["count"])
        for row in connection.execute(
            "SELECT repository, count(*) AS count FROM jobs WHERE status = 'running' GROUP BY repository"
        )
    }
    eligible = [
        row for row in rows
        if set(json.loads(row["labels"])).issubset(labels)
        and (max_repository_running is None or active.get(row["repository"], 0) < max_repository_running)
    ]
    return min(eligible, key=lambda row: (active.get(row["repository"], 0), row["created_at"], row["id"]), default=None)
