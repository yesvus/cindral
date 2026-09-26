"""Durable job queue with leases for direct device execution."""
from collections.abc import Iterable
from dataclasses import dataclass
import json
import sqlite3
import time
from typing import Any
import uuid

PENDING = "pending"
RUNNING = "running"
SUCCESS = "success"
FAILURE = "failure"
TERMINAL = (SUCCESS, FAILURE)
MAX_LOG_CHARS = 64 * 1024


@dataclass(frozen=True)
class Job:
    id: str
    repository: str
    sha: str
    ref: str
    labels: tuple[str, ...]
    timeout: int
    created_at: float
    status: str
    command: tuple[str, ...] = ()
    device: str | None = None
    lease_expires: float | None = None
    exit_code: int | None = None
    attempts: int = 0
    delivery: str | None = None
    status_sha: str | None = None
    log: str = ""

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "repository": self.repository,
            "sha": self.sha,
            "ref": self.ref,
            "command": list(self.command),
            "labels": list(self.labels),
            "timeout": self.timeout,
            "status": self.status,
            "device": self.device,
            "exit_code": self.exit_code,
            "attempts": self.attempts,
            "status_sha": self.status_sha,
            "log": self.log,
        }


def _row(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        repository=row["repository"],
        sha=row["sha"],
        ref=row["ref"],
        command=tuple(json.loads(row["command"])),
        labels=tuple(json.loads(row["labels"])),
        timeout=int(row["timeout"]),
        created_at=float(row["created_at"]),
        status=row["status"],
        device=row["device"],
        lease_expires=row["lease_expires"],
        exit_code=row["exit_code"],
        attempts=int(row["attempts"]),
        delivery=row["delivery"],
        status_sha=row["status_sha"],
        log=row["log"] or "",
    )


class JobStore:
    """SQLite-backed queue. A lease, not a metric, is the slot authority."""

    def __init__(self, path: str) -> None:
        self.path = str(path)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _init(self) -> None:
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    repository TEXT NOT NULL,
                    sha TEXT NOT NULL,
                    ref TEXT NOT NULL,
                    command TEXT NOT NULL,
                    labels TEXT NOT NULL,
                    timeout INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL,
                    device TEXT,
                    lease_expires REAL,
                    exit_code INTEGER,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    delivery TEXT,
                    status_sha TEXT,
                    log TEXT
                )
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "log" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN log TEXT")
            if "status_sha" not in columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN status_sha TEXT")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at)"
            )
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS jobs_delivery ON jobs(delivery) WHERE delivery IS NOT NULL"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('reclaims', 0)")
        finally:
            connection.close()

    def enqueue(
        self,
        repository: str,
        sha: str,
        ref: str,
        command: tuple[str, ...] | list[str] = (),
        labels: tuple[str, ...] | list[str] = (),
        timeout: int = 3600,
        delivery: str | None = None,
        status_sha: str | None = None,
        now: float | None = None,
    ) -> Job:
        if not sha:
            raise ValueError("job requires a commit sha")
        job_id = uuid.uuid4().hex
        created = time.time() if now is None else now
        connection = self._connect()
        try:
            try:
                connection.execute(
                    "INSERT INTO jobs"
                    " (id, repository, sha, ref, command, labels, timeout, created_at, status, delivery, status_sha)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job_id,
                        repository,
                        sha,
                        ref,
                        json.dumps(list(command)),
                        json.dumps(list(labels)),
                        int(timeout),
                        created,
                        PENDING,
                        delivery,
                        status_sha,
                    ),
                )
            except sqlite3.IntegrityError:
                if not delivery:
                    raise
                row = connection.execute(
                    "SELECT * FROM jobs WHERE delivery = ?", (delivery,)
                ).fetchone()
                if row is None:
                    raise
                return _row(row)
        finally:
            connection.close()
        return Job(
            id=job_id,
            repository=repository,
            sha=sha,
            ref=ref,
            command=tuple(command),
            labels=tuple(labels),
            timeout=int(timeout),
            created_at=created,
            status=PENDING,
            delivery=delivery,
            status_sha=status_sha,
        )

    def get(self, job_id: str) -> Job | None:
        connection = self._connect()
        try:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return _row(row) if row is not None else None
        finally:
            connection.close()

    def claim(
        self,
        device: str,
        labels: tuple[str, ...] | list[str],
        lease_seconds: int = 300,
        now: float | None = None,
    ) -> Job | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        claimed_at = time.time() if now is None else now
        advertised = set(labels)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            # return abandoned jobs before handing out work so a device that
            # disappeared does not strand its job in running forever
            cursor = connection.execute(
                "UPDATE jobs SET status = ?, device = NULL, lease_expires = NULL"
                " WHERE status = ? AND lease_expires IS NOT NULL AND lease_expires < ?",
                (PENDING, RUNNING, claimed_at),
            )
            if cursor.rowcount > 0:
                connection.execute(
                    "UPDATE meta SET value = value + ? WHERE key = 'reclaims'",
                    (cursor.rowcount,),
                )
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC",
                (PENDING,),
            ).fetchall()
            picked = next(
                (row for row in rows if set(json.loads(row["labels"])).issubset(advertised)),
                None,
            )
            if picked is None:
                connection.execute("COMMIT")
                return None
            connection.execute(
                "UPDATE jobs SET status = ?, device = ?, lease_expires = ?, attempts = attempts + 1"
                " WHERE id = ? AND status = ?",
                (RUNNING, device, claimed_at + lease_seconds, picked["id"], PENDING),
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return self.get(picked["id"])

    def renew(
        self,
        job_id: str,
        device: str,
        lease_seconds: int = 300,
        now: float | None = None,
    ) -> bool:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        renewed_at = time.time() if now is None else now
        connection = self._connect()
        try:
            cursor = connection.execute(
                "UPDATE jobs SET lease_expires = ?"
                " WHERE id = ? AND status = ? AND device = ?"
                " AND lease_expires IS NOT NULL AND lease_expires > ?",
                (renewed_at + lease_seconds, job_id, RUNNING, device, renewed_at),
            )
            return cursor.rowcount == 1
        finally:
            connection.close()

    def holds_lease(self, job_id: str, device: str, now: float | None = None) -> bool:
        checked_at = time.time() if now is None else now
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT status, device, lease_expires FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return False
            return (
                row["status"] == RUNNING
                and row["device"] == device
                and row["lease_expires"] is not None
                and row["lease_expires"] > checked_at
            )
        finally:
            connection.close()

    def report(
        self,
        job_id: str,
        device: str,
        exit_code: int,
        now: float | None = None,
        log: str = "",
    ) -> Job:
        reported_at = time.time() if now is None else now
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise KeyError(job_id)
            if row["status"] != RUNNING or row["device"] != device:
                connection.execute("ROLLBACK")
                raise ValueError("job is not leased to this device")
            if row["lease_expires"] is None or row["lease_expires"] <= reported_at:
                connection.execute("ROLLBACK")
                raise ValueError("job lease has expired")
            status = SUCCESS if exit_code == 0 else FAILURE
            connection.execute(
                "UPDATE jobs SET status = ?, exit_code = ?, lease_expires = NULL, log = ? WHERE id = ?",
                (status, int(exit_code), log[:MAX_LOG_CHARS] if log else None, job_id),
            )
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        job = self.get(job_id)
        assert job is not None
        return job

    def reclaim_expired(self, now: float | None = None) -> int:
        reclaimed_at = time.time() if now is None else now
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "UPDATE jobs SET status = ?, device = NULL, lease_expires = NULL"
                " WHERE status = ? AND lease_expires IS NOT NULL AND lease_expires < ?",
                (PENDING, RUNNING, reclaimed_at),
            )
            count = cursor.rowcount
            if count > 0:
                connection.execute(
                    "UPDATE meta SET value = value + ? WHERE key = 'reclaims'",
                    (count,),
                )
            connection.execute("COMMIT")
            return count
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def reclaim_count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute("SELECT value FROM meta WHERE key = 'reclaims'").fetchone()
            return int(row["value"]) if row is not None else 0
        finally:
            connection.close()

    def pool_snapshot(
        self,
        runners: Iterable[Any] = (),
        now: float | None = None,
    ) -> dict[str, Any]:
        snapshot_time = time.time() if now is None else now
        connection = self._connect()
        try:
            status_counts = {PENDING: 0, RUNNING: 0, SUCCESS: 0, FAILURE: 0}
            for row in connection.execute(
                "SELECT status, count(*) AS count FROM jobs GROUP BY status"
            ):
                status_counts[row["status"]] = int(row["count"])

            oldest_row = connection.execute(
                "SELECT min(created_at) AS oldest FROM jobs WHERE status = ?",
                (PENDING,),
            ).fetchone()
            oldest_age = 0.0
            if oldest_row and oldest_row["oldest"] is not None:
                oldest_age = max(0.0, snapshot_time - float(oldest_row["oldest"]))

            reclaim_row = connection.execute(
                "SELECT value FROM meta WHERE key = 'reclaims'"
            ).fetchone()
            reclaim_total = int(reclaim_row["value"]) if reclaim_row is not None else 0

            active_lease_rows = connection.execute(
                "SELECT id, repository, sha, ref, device, lease_expires FROM jobs WHERE status = ? AND lease_expires IS NOT NULL AND lease_expires > ?",
                (RUNNING, snapshot_time),
            ).fetchall()
            leases_by_device: dict[str, dict[str, Any]] = {}
            for row in active_lease_rows:
                dev = row["device"]
                if dev:
                    expires = float(row["lease_expires"])
                    leases_by_device[dev] = {
                        "job_id": row["id"],
                        "repository": row["repository"],
                        "sha": row["sha"],
                        "ref": row["ref"],
                        "lease_expires": expires,
                        "remaining_seconds": max(0.0, expires - snapshot_time),
                    }

            devices: list[dict[str, Any]] = []
            seen_devices: set[str] = set()
            for runner in runners:
                dev_name = runner.name
                seen_devices.add(dev_name)
                lease = leases_by_device.get(dev_name)
                devices.append(
                    {
                        "name": dev_name,
                        "status": runner.status,
                        "healthy": runner.healthy,
                        "busy": runner.busy or (lease is not None),
                        "labels": list(runner.labels),
                        "current_lease": lease,
                    }
                )

            for dev_name, lease in leases_by_device.items():
                if dev_name not in seen_devices:
                    devices.append(
                        {
                            "name": dev_name,
                            "status": "online",
                            "healthy": True,
                            "busy": True,
                            "labels": [],
                            "current_lease": lease,
                        }
                    )

            recent_rows = connection.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
            recent_jobs = [_row(row).as_dict() for row in recent_rows]

            return {
                "devices": devices,
                "queue_depth": status_counts,
                "oldest_pending_age_seconds": round(oldest_age, 2),
                "reclaim_count": reclaim_total,
                "success_count": status_counts.get(SUCCESS, 0),
                "failure_count": status_counts.get(FAILURE, 0),
                "recent_jobs": recent_jobs,
            }
        finally:
            connection.close()

    def list(self, status: str | None = None) -> list[Job]:
        connection = self._connect()
        try:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY created_at ASC"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC",
                    (status,),
                ).fetchall()
            return [_row(row) for row in rows]
        finally:
            connection.close()
