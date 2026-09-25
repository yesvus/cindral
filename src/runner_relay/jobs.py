"""Durable job queue with leases for direct device execution."""
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass

PENDING = "pending"
RUNNING = "running"
SUCCESS = "success"
FAILURE = "failure"
TERMINAL = (SUCCESS, FAILURE)


@dataclass(frozen=True)
class Job:
    id: str
    repository: str
    sha: str
    ref: str
    command: tuple[str, ...]
    labels: tuple[str, ...]
    timeout: int
    created_at: float
    status: str
    device: str | None = None
    lease_expires: float | None = None
    exit_code: int | None = None
    attempts: int = 0

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
                    attempts INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, created_at)"
            )
        finally:
            connection.close()

    def enqueue(
        self,
        repository: str,
        sha: str,
        ref: str,
        command: tuple[str, ...] | list[str],
        labels: tuple[str, ...] | list[str] = (),
        timeout: int = 3600,
        now: float | None = None,
    ) -> Job:
        if not command:
            raise ValueError("job requires a command")
        if not sha:
            raise ValueError("job requires a commit sha")
        job_id = uuid.uuid4().hex
        created = time.time() if now is None else now
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO jobs (id, repository, sha, ref, command, labels, timeout, created_at, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ),
            )
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
        claimed_at = time.time() if now is None else now
        advertised = set(labels)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
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
        renewed_at = time.time() if now is None else now
        connection = self._connect()
        try:
            cursor = connection.execute(
                "UPDATE jobs SET lease_expires = ? WHERE id = ? AND status = ? AND device = ?",
                (renewed_at + lease_seconds, job_id, RUNNING, device),
            )
            return cursor.rowcount == 1
        finally:
            connection.close()

    def report(
        self,
        job_id: str,
        device: str,
        exit_code: int,
        now: float | None = None,
    ) -> Job:
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
            status = SUCCESS if exit_code == 0 else FAILURE
            connection.execute(
                "UPDATE jobs SET status = ?, exit_code = ?, lease_expires = NULL WHERE id = ?",
                (status, int(exit_code), job_id),
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
            cursor = connection.execute(
                "UPDATE jobs SET status = ?, device = NULL, lease_expires = NULL"
                " WHERE status = ? AND lease_expires IS NOT NULL AND lease_expires < ?",
                (PENDING, RUNNING, reclaimed_at),
            )
            return cursor.rowcount
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
