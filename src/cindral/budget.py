"""A ceiling on metered dispatches, counted from the broker's own records.

GitHub does not report how long a hosted run took, and a hosted run leaves no
job row here because nothing executes it, so metered minutes cannot be measured
after the fact. What the broker can trust is the dispatches it accepted, counted
from the ledger written at the moment each one was sent. A ceiling in minutes is
converted with an assumed duration per run, which makes the bound pessimistic,
and that is the only safe direction for a limit whose crossing costs money.

The month is UTC, matching the included-minutes reset, so a reset mid-session
lifts the ceiling rather than waiting for local midnight.
"""
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone

from .metering import HOSTED_LANE, REPOSITORY_UNKNOWN


def month_start(epoch: float) -> float:
    """The first instant of the UTC month an instant falls in."""
    moment = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def month_label(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m")


class DispatchBudget:
    """Refuses a metered dispatch once the month reaches its ceiling.

    `minutes_per_run` is what one dispatch is assumed to cost. Raising it lowers
    the number of dispatches allowed, and it is the only knob that trades
    coverage against certainty, so the default is deliberately high.
    """

    def __init__(
        self,
        ledger,
        *,
        monthly_minutes: float,
        minutes_per_run: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if monthly_minutes < 0:
            raise ValueError("monthly minute budget cannot be negative")
        if minutes_per_run <= 0:
            raise ValueError("assumed minutes per run must be positive")
        self._ledger = ledger
        self.monthly_minutes = float(monthly_minutes)
        self.minutes_per_run = float(minutes_per_run)
        self._clock = clock

    def dispatches(self) -> int:
        """How many metered dispatches this UTC month has already been accepted."""
        return self._ledger.count(month_start(self._clock()), HOSTED_LANE)

    def spent_minutes(self) -> float:
        return self.dispatches() * self.minutes_per_run

    def assumed_minutes(self) -> float:
        """What one dispatch is assumed to cost, which the bound is built on."""
        return self.minutes_per_run

    def remaining_minutes(self) -> float:
        return max(0.0, self.monthly_minutes - self.spent_minutes())

    def reached(self, estimated_minutes: float = 0.0) -> bool:
        """Whether one more dispatch would cross the ceiling.

        The estimate is added rather than ignored, so a budget that fits exactly
        one more short run does not admit a long one.
        """
        return self.spent_minutes() + max(0.0, estimated_minutes) >= self.monthly_minutes

    def reason(self) -> str:
        """The user-visible reason a metered lane was refused this month."""
        return (
            f"the {month_label(self._clock())} metered budget of "
            f"{self.monthly_minutes:g} minutes is spent: "
            f"{self.spent_minutes():g} minutes used at {self.minutes_per_run:g} "
            f"minutes per dispatch"
        )


def _admits(policy, used: float, estimated_minutes: float = 0.0) -> bool:
    """Whether the ceiling admits one more dispatch, given a count already read.

    The rule lives here so the check and the report cannot disagree about it. The
    report needs the count and the answer from the same read, because a dispatch
    committed between two reads would leave the reported minutes and the reported
    answer describing different moments, and asking the check for its answer would
    read the ledger a second time. Reimplementing the comparison instead would make
    two implementations of a spending ceiling, and those drift in the direction that
    spends.
    """
    assumed = policy.assumed_minutes_per_run
    cost = max(0.0, estimated_minutes or 0.0, assumed or 0.0)
    return used + cost < policy.minute_budget


def budget_snapshot(policy) -> dict[str, object] | None:
    """What the metered month looks like right now, or None when there is no ceiling.

    The refusal names the month and the minutes used, but only once a dispatch has
    been turned away, which makes the ceiling readable exactly when it has already
    cost something. An operator who wants to know whether the month is nearly spent
    has no way to ask, so the same figures are reported on a read surface whether or
    not anything is currently being refused.
    """
    if policy.minute_budget is None or policy.metered_minutes is None:
        return None
    try:
        used = policy.metered_minutes()
    except Exception as exc:  # noqa: BLE001 - a probe must answer
        # The count is a SQLite read, and this is reported on a health probe, which
        # is the one request that has to produce an answer. A read that fails must
        # not turn into a failed health check, or a locked database takes the pod
        # out of rotation for a figure nobody can read. The figure is omitted and
        # the reason is named, so "I cannot tell" is visibly different from "the
        # month is empty".
        return {"unavailable": f"the metered count could not be read: {exc}"}
    # from the same read as the figures above it, so a dispatch committed in
    # between cannot leave the count and the answer describing different moments
    admitted = _admits(policy, used)
    return {
        "month": month_label(time.time()),
        "budget_minutes": policy.minute_budget,
        "used_minutes": used,
        "remaining_minutes": max(0.0, policy.minute_budget - used),
        "assumed_minutes_per_dispatch": policy.assumed_minutes_per_run,
        # Remaining minutes are arithmetic, not permission: at 25 minutes per
        # dispatch a month with 25 remaining admits nothing, because admitting one
        # would cross the ceiling. Reporting only the remainder tells an operator
        # there is room when there is none, which is the reading this figure exists
        # to replace. Asked of the same call that refuses a dispatch, so the two
        # cannot disagree.
        "next_dispatch_admitted": admitted,
    }


def budget_refusal(policy, estimated_minutes: float = 0.0) -> str | None:
    """The reason a metered dispatch is refused, or None to allow it.

    Ahead of every hosted return, including an explicit request, because a
    caller naming the hosted lane is exactly the case a limit exists to cover.

    An estimate below the cost the ceiling is already assuming is raised to it.
    The bound is a count of dispatches converted at a fixed duration, so a
    caller that supplies no estimate, or a small one, would otherwise buy a
    dispatch that the arithmetic says does not fit.
    """
    if policy.minute_budget is None or policy.metered_minutes is None:
        return None
    used = policy.metered_minutes()
    if _admits(policy, used, estimated_minutes):
        return None
    assumed = policy.assumed_minutes_per_run
    return (
        f"the metered budget of {policy.minute_budget:g} minutes is spent: "
        f"{used:g} minutes used this month at {assumed:g} minutes per dispatch"
    )


def reserve_metered(policy, estimated_minutes: float = 0.0) -> tuple[int | None, str | None]:
    """Check the ceiling and hold the cost of this dispatch, under one lock.

    Reading the spend and writing the hold have to be the same step. A check that
    runs on its own and a record that is written later let two simultaneous
    requests both see the same remaining budget and both go through, which is
    exactly the case a ceiling exists to stop.

    The hold is released by `release_reservation` when the dispatch does not
    happen, because a dispatch GitHub refused was never charged.
    """
    if policy.minute_budget is None or policy.metered_minutes is None:
        return None, None
    reserve = policy.reserve_metered_minutes
    if reserve is None:
        return None, budget_refusal(policy, estimated_minutes)
    with policy.budget_lock:
        used = policy.metered_minutes()
        assumed = policy.assumed_minutes_per_run
        cost = max(0.0, estimated_minutes or 0.0, assumed or 0.0)
        if used + cost >= policy.minute_budget:
            return None, (
                f"the metered budget of {policy.minute_budget:g} minutes is spent: "
                f"{used:g} minutes used this month"
            )
        return reserve(cost), None


def release_hold(policy, reservation: int | None) -> None:
    """Give a hold back through the policy that took it."""
    release = policy.release_metered_minutes
    if release is not None:
        release(reservation)


def release_reservation(server, reservation: int | None) -> None:
    """Give a hold back, when the dispatch it was taken for did not go."""
    ledger = getattr(server, "metered", None)
    if ledger is None:
        return
    try:
        ledger.release(reservation)
    except Exception as exc:  # noqa: BLE001 - a stale hold costs a dispatch, not a run
        print(f"warning: a metered hold could not be released: {exc}", flush=True)


def attach_budget(server, ledger) -> "DispatchBudget | None":
    """Point a policy at its own metered count, when a ceiling is configured.

    The count is a closure over the ledger rather than a value read at startup,
    so it stays current across a month reset without a reload and costs nothing
    to keep. A policy with no ledger is a policy whose ceiling cannot be
    enforced, so that is refused loudly rather than left looking like a limit
    nobody reads.
    """
    policy = server.policy
    if policy.minute_budget is None:
        return None
    if ledger is None:
        raise ValueError(
            "a metered minute budget is configured but there is no ledger to count it,"
            " so nothing could stop the spend"
        )
    budget = DispatchBudget(
        ledger,
        monthly_minutes=policy.minute_budget,
        minutes_per_run=policy.minutes_per_run,
    )
    server.policy = replace(
        policy,
        metered_minutes=budget.spent_minutes,
        assumed_minutes_per_run=budget.assumed_minutes(),
        # the hold is taken for the ceiling's own arithmetic, not the estimate, so
        # the ledger records what the bound assumed rather than what a caller hoped
        reserve_metered_minutes=lambda _cost: ledger.reserve(REPOSITORY_UNKNOWN, HOSTED_LANE),
        release_metered_minutes=ledger.release,
    )
    return budget
