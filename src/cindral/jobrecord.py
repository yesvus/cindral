"""The stored record of one direct-execution run, and how a row maps onto it.

Kept apart from the store so the record can be read, serialized, and rendered
without opening a connection to the queue.
"""
from dataclasses import dataclass
import json
import sqlite3

from .steps import decode_steps


def head_sha_for(sha: str, status_sha: str | None = None) -> str:
    """Which commit a run's result is recorded against.

    A push tests and reports one commit, so both names are it. A pull request tests
    the merge ref and reports on the branch head, because a check on the merge
    commit is not a check the reviewer sees, and the two differ on every pull
    request run by design.

    So the head is not a sign of a wrong commit and must not be refused as one. What
    it must never be is a value resolved when the result is published rather than one
    the run recorded: a check written against whatever the branch pointed at by then
    is attributed to a commit nobody tested, which is indistinguishable afterwards
    from a correct one. Both values therefore come from the stored row, and every
    caller reads them from here so that a fourth source of a commit cannot appear in
    one place and be resolved fresh in another.
    """
    return status_sha or sha


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
    last_error: str | None = None
    # per-step records parsed on the device, kept beside the log rather than
    # inside it so a reader can open one failing step without the whole run
    steps: tuple[dict, ...] = ()

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
            "last_error": self.last_error,
            "status_sha": self.status_sha,
            "log": self.log,
            # the panel's Dispatched column renders only when this is present, so
            # a run that was never dispatched is distinguishable from one whose
            # dispatch time the broker has not reported
            "created_at": self.created_at,
            # empty for a run that reported no step, which is a run that died
            # before the first one rather than a run with no steps
            "steps": [dict(step) for step in self.steps],
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
        last_error=row["last_error"],
        steps=tuple(decode_steps(row["steps"])),
    )
