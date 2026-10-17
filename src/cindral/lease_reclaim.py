"""What a lapsed lease does to a run, and the commit status it owes.

A lease lapsing is the one moment the broker learns a device is gone, so the
release and the status it owes are written in the same transaction. The run
becomes claimable again only after its verdict is durable, which is what stops a
slow post about the earlier loss from overtaking a later real result.

A lease that lapses is not spent at once. A device still announcing itself is
present, so a renewal or a report that arrives late belongs to a device that is
working, and releasing its run would start the same work a second time. That
window buys the late statement a chance to arrive and nothing else: a device
that has stopped renewing will not renew again however long the queue waits, so
the window is bounded and the reclaim records when it had to take a run back
from a device that was still there.

`reclaim_tick` is one pass and `reclaim_loop` is that pass on a timer. The claim
endpoint also runs a tick, because a lapsed lease is noticed the moment a device
asks for work and there is no reason to wait for the timer to see it. Both posts
go through `post_if_current`, so the two drains cannot contradict each other.
"""
from collections.abc import Callable
import json
import sqlite3
import threading
import time
from typing import Any, Protocol

from .agent import ONLINE_WINDOW_SECONDS
from .jobrecord import Job, _row, head_sha_for
from .retry import LEASE_LOST, MAX_ERROR_CHARS, backoff_seconds, parked
from .status_ledger import JobQueue
from .status_text import lost_description

RUNNING = "running"
PENDING = "pending"
DEAD = "dead"
# an agent re-announces every 15s, so presence that has not come back in a month
# is a retired device rather than a host that is down, and would otherwise sit in
# every snapshot as a device with no agent
AGENT_RETENTION_SECONDS = 30 * 24 * 60 * 60
# a claim is the fast path, so this is a backstop. It matches the webhook replay
# loop, and a lease is already five minutes old before it can be spent at all.
RECLAIM_INTERVAL_SECONDS = 60.0
# the window the pool uses to call a device online, so "the device is present"
# means the same thing here as it does in the snapshot. It is a grace for a late
# renewal or a late report, not evidence that the run is progressing: the two
# heartbeat threads a device runs are independent, so a wedged executor with a
# dead renewal thread keeps announcing the device and stops the run.
#: The status post runs inside the lock that guards the ordering, and the claim
#: endpoint takes that lock too, so its timeout is the ceiling on how long a
#: device can be kept waiting for work. Well under the client budgets, which
#: give up at 15 and 30 seconds, because a device that loses its claim loses its
#: run.
POST_TIMEOUT_SECONDS = 8

LIVE_DEVICE_GRACE_SECONDS = ONLINE_WINDOW_SECONDS


def spent_lease(now: float) -> tuple[str, tuple[float, ...]]:
    """The condition and its values that mark a lease spent.

    One rule with four readers: the reclaim, a late renewal, a late report, and
    the pool's queue depth. Written once so what the queue calls lost and what
    it calls held cannot disagree.

    A lease is spent once it has lapsed and its device has nothing left to say
    about it. `renew` refreshes `lease_expires` itself, so a lease that has
    lapsed is a run the device has not renewed since its own deadline, whether
    or not the device is still announcing: there is no further signal in the row
    to tell a device that is late from one that has stopped.
    """
    cutoff = now - LIVE_DEVICE_GRACE_SECONDS
    return (
        "lease_expires IS NOT NULL AND lease_expires < ?"
        " AND (lease_expires < ? OR device IS NULL"
        " OR device NOT IN (SELECT device FROM agents WHERE last_seen >= ?))",
        (now, cutoff, cutoff),
    )


def prune_agents(connection: sqlite3.Connection, now: float) -> None:
    """Drop presence for agents that stopped announcing past the retention window."""
    connection.execute("DELETE FROM agents WHERE last_seen < ?", (now - AGENT_RETENTION_SECONDS,))


def release(
    connection: sqlite3.Connection,
    job_id: str,
    attempts: int,
    now: float,
    error: str,
    pending_status: str | None = None,
) -> None:
    """Send a run the device lost back to the queue, or park it out of attempts.

    A device that died leaves no verdict to act on, so the run is not a failed
    test: it waits out a backoff and is claimed again, and only a run past the
    attempt cap stops, in a state that is listed and never handed out.

    `pending_status` is written in the same transaction as the release, so the
    verdict this owes the commit is durable before the run is claimable again.
    A status that fails to publish later is still owed, and a later queue event
    republishes it.
    """
    connection.execute(
        "UPDATE jobs SET status = ?, device = NULL, lease_expires = NULL,"
        " last_error = ?, available_at = ?, pending_status = COALESCE(?, pending_status),"
        " status_seq = status_seq + ?"
        " WHERE id = ?",
        (
            DEAD if parked(attempts) else PENDING,
            error[:MAX_ERROR_CHARS],
            now + backoff_seconds(attempts),
            pending_status,
            1 if pending_status is not None else 0,
            job_id,
        ),
    )


def _count_reclaims(connection: sqlite3.Connection, lapsed: list[sqlite3.Row], now: float) -> None:
    """Record the reclaims, split by whether the holding device was still there.

    Nothing else records which of the two happened, and the split is the answer
    to whether a run was lost or restarted: a lease taken back from a device
    that kept beating is the second, and it is the one that duplicates work.
    """
    connection.execute("UPDATE meta SET value = value + ? WHERE key = 'reclaims'", (len(lapsed),))
    cutoff = now - LIVE_DEVICE_GRACE_SECONDS
    present = {
        row["device"]
        for row in connection.execute("SELECT device FROM agents WHERE last_seen >= ?", (cutoff,))
    }
    live = sum(1 for row in lapsed if row["device"] in present)
    # upserted rather than updated in place, so a queue created before this
    # counter existed records its first such reclaim instead of dropping it
    connection.execute(
        "INSERT INTO meta (key, value) VALUES ('reclaims_live', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = value + ?",
        (live, live),
    )


def expire(
    connection: sqlite3.Connection,
    now: float,
    publish: Callable[[Job], str] | None = None,
) -> list[Job]:
    """Release every run whose lease is spent, so a dead device strands none.

    A heartbeat that stopped says the device is gone, and the lapsed lease is
    what turns that into a retry or, past the attempt cap, a parked run. A lease
    under a device that is still announcing is left alone until the grace is
    spent, because releasing it hands the same run to a second device while the
    first one is still on it.

    `publish` computes the commit status the release owes, which is recorded in
    the same transaction. Passing it is what makes the release ordered: the run
    carries its owed verdict before it can be claimed again, so a later report
    cannot be overtaken by a slow post about the earlier loss.
    """
    spent, bounds = spent_lease(now)
    lapsed = connection.execute(
        f"SELECT id, device, attempts FROM jobs WHERE status = ? AND {spent}", (RUNNING, *bounds)
    ).fetchall()
    if not lapsed:
        return []
    marks = ", ".join("?" * len(lapsed))
    found = connection.execute(f"SELECT * FROM jobs WHERE id IN ({marks})", [row["id"] for row in lapsed])
    expired = [_row(row) for row in found]
    owed = {job.id: publish(job) for job in expired} if publish is not None else {}
    for job in expired:
        release(connection, job.id, job.attempts, now, LEASE_LOST, owed.get(job.id))
    _count_reclaims(connection, lapsed, now)
    return expired


def status_publisher(
    github: Any, context: str, target_url: Callable[[str], str]
) -> Callable[[Job, str, str], None]:
    """Build the publisher a tick posts through, one commit status per call.

    A broker with no GitHub client still has to reclaim, so the queue is handed a
    publisher that accepts every verdict: there is nowhere to send it, and
    holding it owed forever would grow the queue instead of emptying it.
    """
    if github is None:
        return lambda job, state, description: None

    def publish(job: Job, state: str, description: str) -> None:
        github.post_status(
            job.repository,
            head_sha_for(job.sha, job.status_sha),
            state,
            description,
            context=context,
            target_url=target_url(job.id),
            timeout=POST_TIMEOUT_SECONDS,
        )

    return publish



def check_run_publisher(
    github: Any, target_url: Callable[[str], str]
) -> Callable[[Job], None]:
    """Build the callable that closes the check a run was opened with.

    A check opened when the run was queued has to be closed by whichever path
    settles the run, or the commit shows a check in progress forever while its
    status says the run is lost. This runs outside the lock that orders the
    verdicts, because a check run is two requests with a long timeout and the
    ordering is about the status.
    """
    if github is None:
        return lambda job: None

    def close_check(job: Job) -> None:
        # imported here because check_runs imports this module, and the cycle is
        # only visible at import time
        from .check_runs import publish_check_run_quietly

        publish_check_run_quietly(github, job, target_url=target_url(job.id))

    return close_check


def reclaim_tick(
    store: JobQueue,
    publish: Callable[[Job, str, str], None],
    now: float | None = None,
    check: Callable[[Job], None] | None = None,
    *,
    discard: Callable[[str], None] | None = None,
) -> int:
    """Release every spent lease, then deliver the verdicts the queue still owes.

    The claim endpoint runs the same pair of steps for the device asking for
    work. Running them on a timer is what stops a lapsed lease from waiting for
    a claim an idle pool never makes, and what carries a parked run's verdict to
    its commit when no device comes back to drain it.

    The drain follows the release in the same tick, and it takes each verdict out
    of the queue before posting it. Taking it is what stops two drains sending
    the same verdict twice, and it is also what stops a run that was re-claimed
    and finished while the post was in flight from being told afterwards that it
    was lost: a verdict that has been superseded is not in the row any more, so
    there is nothing stale left to send.
    """
    reclaimed_at = time.time() if now is None else now
    released = release_expired(store, now=reclaimed_at, discard=discard)
    drain_owed(store, publish, check)
    return len(released)


def release_expired(
    store: JobQueue,
    publish: Callable[[Job], str] | None = None,
    now: float | None = None,
    discard: Callable[[str], None] | None = None,
) -> list[Job]:
    """Release every spent lease, and what the release takes with it.

    Both callers go through here rather than releasing for themselves, because a
    release on its own is incomplete: a run about to execute again cannot keep the
    archive the attempt that lost its lease produced, or a reader downloads those
    bytes under the new attempt's verdict. The discard is best-effort and never
    raises, because a reclaim must not fail over a file it is tidying.
    """
    released = store.reclaim_expired(
        now=time.time() if now is None else now,
        publish=publish or (lambda job: json.dumps(lost_description(job, LEASE_LOST))),
    )
    if discard is not None:
        for job in released:
            try:
                discard(job.id)
            except Exception:  # noqa: BLE001 - a release is not a tidying failure
                pass
    return released


def drain_owed(
    store: JobQueue,
    publish: Callable[..., None],
    check: Callable[[Job], None] | None = None,
) -> None:
    """Post every verdict the queue owes, one at a time, and keep the rest.

    Each verdict is posted and restored on its own, so a post that fails costs
    that verdict and not the ones behind it. The claim is still one read, so a row
    that cannot be converted stalls the pass rather than skipping itself, which is
    the safe direction: nothing is lost, and the next pass reads it again.
    """
    for job, status, seq in store.claim_owed_statuses():
        try:
            if post_if_current(store, job, status, seq, publish) and check is not None:
                # Outside the lock the post ran in. A check run makes two requests
                # of its own, and holding the ordering lock across it would stall
                # every claim for a minute over a status the commit already has.
                check(job)
        except Exception as exc:  # noqa: BLE001 - one unreadable row, not the rest
            _restore(store, job, status, seq)
            print(f"warning: commit status for job {job.id} is still owed: {exc}", flush=True)


def _restore(store, job, status: str, seq: int) -> None:
    """Put one verdict back, and say so if even that fails."""
    try:
        store.restore_status(job.id, status, seq)
    except Exception as exc:  # noqa: BLE001 - a lost verdict must be visible
        print(f"warning: the verdict for job {job.id} was lost: {exc}", flush=True)


def post_if_current(store, job, status: str, seq: int, publish: Callable[..., None]) -> bool:
    """Post a verdict only if it is still the latest this run has been given.

    Two drains can hold verdicts for the same run: the timer and a request. The
    one that started first can finish last, and a run that has been reclaimed
    again would then be reported lost on the strength of the first loss. The
    sequence number is the check, and it is taken at the moment of the post
    rather than at the moment of the claim, because that is where the ordering
    goes wrong.
    """
    state, description = json.loads(status)
    # The lock spans the check and the post, and everything that writes a verdict
    # takes it too. Holding it only across the check would be the same bug one
    # step later: a newer verdict arrives in the gap and this one still lands on
    # top of it.
    with store.status_lock:
        if store.status_seq(job.id) != seq:
            # a verdict arrived while this one waited, and the later text is the
            # run's real state. The older one is superseded rather than lost, so
            # it does not go back into the queue.
            return False
        try:
            publish(job, state, description)
        except Exception as exc:  # noqa: BLE001 - the verdict goes back and stays owed
            store.restore_status(job.id, status, seq)
            print(f"warning: commit status for job {job.id} is still owed: {exc}", flush=True)
            return False
    return True


def reclaim_loop(
    store: JobQueue,
    publish: Callable[[Job, str, str], None],
    stop: threading.Event,
    interval: float = RECLAIM_INTERVAL_SECONDS,
    check: Callable[[Job], None] | None = None,
    *,
    discard: Callable[[str], None] | None = None,
) -> None:
    """Reclaim on a timer, so the queue settles on its own rather than on traffic.

    The body runs before the first wait, so a broker that comes back finds the
    leases its previous process left behind spent immediately rather than a
    minute later. A tick that raises is reported and the loop continues: a
    transient failure is the one thing that must not end the guarantee.
    """
    while not stop.is_set():
        try:
            reclaim_tick(store, publish, check=check, discard=discard)
        except Exception as exc:  # noqa: BLE001 - a tick that raised must not stop the loop
            print(f"warning: lease reclaim paused: {exc}", flush=True)
        stop.wait(interval)
