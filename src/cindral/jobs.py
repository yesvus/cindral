"""Durable queue, leases, and agent presence for direct device execution."""
from collections.abc import Callable
import json
import sqlite3
import threading
import time
from typing import Any
import uuid

from .jobrecord import Job, _row
from .schema import prepare
from .agents_table import record_heartbeat
from .verdict_states import MAX_LOG_CHARS
from .verdicts import settle
from .lease_reclaim import (
    AGENT_RETENTION_SECONDS,
    DEAD,
    PENDING,
    RUNNING,
    expire,
    prune_agents,
    release,
    spent_lease,
)
from .sole_holder import take
from .status_ledger import (
    claim_owed,
    clear_status,
    current_seq,
    owe_status,
    owed_statuses,
    restore_status,
)
from .retry import LEASE_LOST, MAX_ERROR_CHARS, backoff_seconds, parked
from .scheduling import pick_job
from .steps import encode_steps
from .sqlite_store import open_database

SUCCESS = "success"
FAILURE = "failure"
# a run no device finished, past the attempts the queue spends on it: it stays
# listed for a maintainer to read and is never handed out again. DEAD, PENDING,
# and RUNNING live in lease_reclaim, which owns what a lapsed lease does.
TERMINAL = (SUCCESS, FAILURE, DEAD)
RECENT_JOB_LIMIT = 50
# the panel renders a preview, not the full stored log, so a snapshot stays
# small enough to serve inline instead of shipping megabytes of CI output
SNAPSHOT_LOG_CHARS = 4000

class JobStore:
    """SQLite-backed queue. A lease, not a metric, is the slot authority."""

    def __init__(self, path: str, sole_holder: bool = True) -> None:
        # Held for as long as this store is open. The status ordering is guarded
        # by a process-local lock, so one queue has one broker; a second one
        # pointed at the same file refuses to start rather than posting verdicts
        # against a row it does not own.
        self._hold = take(f"{path}.holder") if sole_holder else None
        # Held across "is this still the latest verdict" and the post that
        # follows it, and by everything that writes a verdict. Without it the
        # check and the post are two steps and a newer verdict can arrive
        # between them, which is the ordering this lock exists to prevent.
        self.status_lock = threading.RLock()
        self.path = str(path)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        return open_database(self.path, timeout=30)

    def _init(self) -> None:
        connection = self._connect()
        try:
            prepare(connection)
        finally:
            connection.close()

    def record_heartbeat(
        self,
        device: str,
        labels: tuple[str, ...] | list[str],
        max_concurrency: int,
        now: float | None = None,
        build: dict[str, str] | None = None,
    ) -> float:
        """Record that a device is there, and what it is running.

        Recorded in the database so a restart keeps a live device listed, and so
        the build it reports outlives the process that heard it.
        """
        return self._write(
            lambda connection: record_heartbeat(
                connection, device, labels, max_concurrency, now, build
            )
        )

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
            columns = "id, repository, sha, ref, command, labels, timeout, created_at, status, delivery, status_sha"
            values = (job_id, repository, sha, ref, json.dumps(list(command)),
                      json.dumps(list(labels)), int(timeout), created, PENDING, delivery, status_sha)
            placeholders = ", ".join("?" * len(columns.split(",")))
            try:
                connection.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", values)
            except sqlite3.IntegrityError:
                if not delivery:
                    raise
                row = connection.execute("SELECT * FROM jobs WHERE delivery = ?", (delivery,)).fetchone()
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
        def fetch(connection: sqlite3.Connection) -> Job | None:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return _row(row) if row is not None else None

        return self._read(fetch)

    def claim(
        self,
        device: str,
        labels: tuple[str, ...] | list[str],
        lease_seconds: int = 300,
        now: float | None = None,
        max_repository_running: int | None = None,
    ) -> Job | None:
        """Hand out one run, if any is eligible.

        This hands out work and nothing else. `reclaim_expired` is the only
        release path, and it is the one that records what a release owes.
        """
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_repository_running is not None and max_repository_running <= 0:
            raise ValueError("max_repository_running must be positive")
        claimed_at = time.time() if now is None else now
        advertised = set(labels)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status = ?"
                " AND (available_at IS NULL OR available_at <= ?) ORDER BY created_at ASC",
                (PENDING, claimed_at),
            ).fetchall()
            picked = pick_job(connection, rows, advertised, max_repository_running)
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
        # a renewal that arrives after the expiry is the one worth having: the
        # device is working, and refusing it is what restarted its run
        spent, bounds = spent_lease(renewed_at)
        connection = self._connect()
        try:
            cursor = connection.execute(
                f"UPDATE jobs SET lease_expires = ?"
                f" WHERE id = ? AND status = ? AND device = ? AND NOT ({spent})",
                (renewed_at + lease_seconds, job_id, RUNNING, device, *bounds),
            )
            return cursor.rowcount == 1
        finally:
            connection.close()

    def holds_lease(self, job_id: str, device: str, now: float | None = None) -> bool:
        checked_at = time.time() if now is None else now
        spent, bounds = spent_lease(checked_at)
        held = self._read(
            lambda connection: connection.execute(
                f"SELECT 1 FROM jobs WHERE id = ? AND status = ? AND device = ? AND NOT ({spent})",
                (job_id, RUNNING, device, *bounds),
            ).fetchone()
        )
        return held is not None

    def report(
        self,
        job_id: str,
        device: str,
        exit_code: int | None = None,
        now: float | None = None,
        log: str = "",
        error: str = "",
        steps: list[dict] | None = None,
    ) -> Job:
        """Record a verdict for a leased run, or the device error that ended it.

        An exit code is the run's own answer, so it is final. An error says the
        device never got that far, which is retried and not recorded as a
        failure, because a red commit nobody can investigate is worse than a
        missing one. Reporting neither is refused: the queue will not invent a
        verdict for a run that produced none.
        """
        reported_at = time.time() if now is None else now
        with self.status_lock:
            connection = self._connect()
            try:
                settle(connection, job_id, device, exit_code, reported_at, log, error, steps)
            finally:
                connection.close()
        job = self.get(job_id)
        assert job is not None
        return job

    def reclaim_expired(
        self,
        now: float | None = None,
        publish: Callable[[Job], str] | None = None,
    ) -> list[Job]:
        """Return the runs whose lease lapsed, retried or parked as their attempts allow.

        The release is one immediate transaction, so two claims arriving together
        cannot both requeue the same run and count it twice. With `publish`, the
        commit status the release owes is written in that same transaction, so
        the ordering between the loss and any later report is fixed by the
        write rather than by the speed of a network call.
        """
        reclaimed_at = time.time() if now is None else now
        with self.status_lock:
            return self._reclaim(reclaimed_at, publish)

    def _reclaim(self, reclaimed_at: float, publish: Callable[[Job], str] | None) -> list[Job]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            released = expire(connection, reclaimed_at, publish)
            connection.execute("COMMIT")
            return released
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def owed_statuses(self) -> list[tuple[Job, str]]:
        return self._read(owed_statuses)

    def claim_owed_statuses(self) -> list[tuple[Job, str, int]]:  # noqa: D401
        """Take the owed verdicts, so one drain posts each of them once.

        The sequence number comes back with each one so a post can check, at the
        moment it goes out, that it still holds the latest verdict for that run.
        """
        with self.status_lock:
            return self._write(claim_owed)

    def status_seq(self, job_id: str) -> int:
        """How many verdicts this run has been given."""
        return self._read(lambda connection: current_seq(connection, job_id))

    def restore_status(self, job_id: str, status: str, seq: int) -> None:
        """Put a verdict back after a failed post, unless a newer one took it."""
        with self.status_lock:
            self._write(lambda connection: restore_status(connection, job_id, status, seq))

    def owe_status(self, job_id: str, status: str) -> None:
        """Note a verdict that could not be sent, for the next drain to send."""
        with self.status_lock:
            self._write(lambda connection: owe_status(connection, job_id, status))

    def clear_status(self, job_id: str) -> None:
        """Drop an owed status once it reached the commit, or was superseded."""
        with self.status_lock:
            self._write(lambda connection: clear_status(connection, job_id))

    def reclaim_count(self) -> int:
        connection = self._connect()
        try:
            row = connection.execute("SELECT value FROM meta WHERE key = 'reclaims'").fetchone()
            return int(row["value"]) if row is not None else 0
        finally:
            connection.close()

    def _read(self, build: Callable[[sqlite3.Connection], Any]) -> Any:
        connection = self._connect()
        try:
            return build(connection)
        finally:
            connection.close()

    def _write(self, apply: Callable[[sqlite3.Connection], Any]) -> Any:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = apply(connection)
            connection.execute("COMMIT")
            return result
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def list(self, status: str | None = None) -> list[Job]:
        def fetch(connection: sqlite3.Connection) -> list[Job]:
            where, params = ("", ()) if status is None else (" WHERE status = ?", (status,))
            rows = connection.execute(
                f"SELECT * FROM jobs{where} ORDER BY created_at ASC", params
            ).fetchall()
            return [_row(row) for row in rows]

        return self._read(fetch)
