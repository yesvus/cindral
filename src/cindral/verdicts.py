"""What a verdict does to a run, as one transaction.

Separate from the store because the rules here are about a verdict rather than
about the queue: a run that finished inside its grace is a verdict and not a loss,
a report needs an exit code or a device error and never neither, and a verdict
that completes a run supersedes anything the queue still owes for it.
"""
import json
import sqlite3

from .lease_reclaim import RUNNING, release, spent_lease
from .steps import encode_steps
from .verdict_states import FAILURE, MAX_LOG_CHARS, SUCCESS


def settle(
    connection: sqlite3.Connection,
    job_id: str,
    device: str,
    exit_code: int | None,
    reported_at: float,
    log: str,
    error: str,
    steps: list[dict] | None,
) -> None:
    """Record a verdict for a leased run, or the device error that ended it.

    The caller owns the connection and the transaction boundary; this is the
    body of one, and it either lands or it raises.
    """
    connection.execute("BEGIN IMMEDIATE")
    try:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            connection.execute("ROLLBACK")
            raise KeyError(job_id)
        if row["status"] != RUNNING or row["device"] != device:
            connection.execute("ROLLBACK")
            raise ValueError("job is not leased to this device")
        # a run that finished inside the grace is a verdict, not a loss, and
        # refusing it here is what made a completed build run again
        spent, bounds = spent_lease(reported_at)
        unspent = connection.execute(
            f"SELECT 1 FROM jobs WHERE id = ? AND NOT ({spent})", (job_id, *bounds)
        ).fetchone()
        if unspent is None:
            connection.execute("ROLLBACK")
            raise ValueError("job lease has expired")
        if error:
            # The device says what happened to it, so it is the run's state, and
            # it supersedes the loss the queue still owes for the earlier lease.
            # Without this the sequence does not move and the drain posts that
            # loss afterwards, reporting a device error as a lost run.
            connection.execute(
                "UPDATE jobs SET pending_status = NULL, status_seq = status_seq + 1"
                " WHERE id = ?",
                (job_id,),
            )
            release(connection, job_id, int(row["attempts"]), reported_at, error)
        elif exit_code is None:
            connection.execute("ROLLBACK")
            raise ValueError("a report needs an exit code or a device error")
        else:
            connection.execute(
                # A verdict that completes the run is the run's real outcome,
                # so it supersedes anything the queue still owes for it: the
                # loss of an earlier lease that a device came back and
                # finished. The sequence moves so a post already in hand knows.
                "UPDATE jobs SET status = ?, exit_code = ?, lease_expires = NULL,"
                " last_error = NULL, log = ?, steps = ?, status_seq = status_seq + 1,"
                " pending_status = NULL WHERE id = ?",
                (
                    SUCCESS if exit_code == 0 else FAILURE,
                    int(exit_code),
                    log[:MAX_LOG_CHARS] if log else None,
                    json.dumps(encode_steps(steps)) if steps else None,
                    job_id,
                ),
            )
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
