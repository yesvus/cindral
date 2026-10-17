"""What a metered dispatch costs the broker, on the delivery path.

Separate from the delivery because a hold is not a delivery: it is a reservation
against a ceiling, taken before a dispatch is attempted and given back when the
dispatch does not happen. Only a definite answer gives it back, because an
unreleased hold makes a ceiling one dispatch tight and an erased one books a run
nobody paid for.
"""
from collections.abc import Callable

from .budget import reserve_metered
from .deliveries import Delivery
from .metering import HOSTED_LANE, MeteredLedger


def delivery_fallback(server) -> "Callable[[Delivery], None] | None":
    """Build the hand-off the replay loop offers a refused delivery.

    Supplied by the broker, because only it knows where a commit runs on a
    device. It is a plain callable and not the handler's own enqueue: a replay
    has no request to answer, so it enqueues and says nothing, and a delivery the
    broker has already answered cannot be re-sent to find out.
    """
    jobs = getattr(server, "jobs", None)
    if jobs is None:
        return None

    def queue(record: Delivery) -> None:
        job = jobs.enqueue(record.repository, record.sha, record.ref)
        print(
            f"warning: delivery {record.delivery} for {record.repository} was queued"
            f" on the device pool as {job.id}: the dispatch was refused",
            flush=True,
        )

    return queue


def reserve_for(policy, lane: str | None) -> tuple[int | None, str | None]:
    """Hold the cost of one replayed dispatch, when the replay is metered.

    A lane that costs nothing is not held, because a hold for a free dispatch
    would spend the budget of a lane nobody is paying for.
    """
    if lane != HOSTED_LANE:
        return None, None
    return reserve_metered(policy, 0.0)


def _release(ledger: MeteredLedger | None, reservation: int | None) -> None:
    """Give a hold back, when the dispatch it was taken for did not start."""
    if ledger is None or reservation is None:
        return
    try:
        ledger.release(reservation)
    except Exception as exc:  # noqa: BLE001 - a stale hold costs a dispatch, not a run
        print(f"warning: a metered hold could not be released: {exc}", flush=True)


def _attribute(ledger: MeteredLedger | None, reservation: int | None, record: Delivery) -> None:
    """Point a metered hold at the dispatch it turned out to be for.

    Best effort by design: the run has already started, so a ledger that cannot
    be written is logged and the next ceiling is one under, which is the
    direction that costs rather than the direction that saves.
    """
    if ledger is None or reservation is None:
        return
    try:
        ledger.attribute(reservation, record.repository, record.workflow, record.ref)
    except Exception as exc:  # noqa: BLE001 - see above
        print(f"warning: metered dispatch for {record.repository} was not named: {exc}", flush=True)


