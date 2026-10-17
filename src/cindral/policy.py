"""Deterministic runner selection and dispatch target policy."""
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field as dataclasses_field, replace
import threading
from pathlib import Path
from typing import Any, Mapping
import math
import re
import time
import tomllib

from .budget import release_hold, reserve_metered
from .metering import HOSTED_LANE
from .dispatch_targets import (
    BRANCH_REF_ROOT,
    PUSH_LANES,
    SERVED_MODES,
    _ref_permitted,
    load_dispatch_targets,
)
from .github import is_repository_slug
from .models import DispatchTarget, RouteDecision, RouteRequest, Runner
from .quota import QUOTA_UNKNOWN, QuotaReading, QuotaSource


class RouteUnavailable(ValueError):
    """Raised when policy cannot select an eligible runner."""


#: What one metered dispatch costs when the policy does not say. Deliberately
#: high: overestimating refuses a dispatch that would have been affordable, while
#: underestimating spends past the ceiling, and only one of those costs money.
DEFAULT_MINUTES_PER_RUN = 10.0


def _read_minute_budget(quota: Mapping[str, Any]) -> float | None:
    """The month's metered ceiling, or None when the policy sets none.

    None rather than zero, because a zero budget would refuse every dispatch and
    a policy that means it can say so. Not-a-number and infinity are refused
    rather than carried: every comparison against them is false, so a ceiling
    holding one admits every metered dispatch while reading as a limit.
    """
    if "monthly_minute_budget" not in quota:
        return None
    try:
        budget = float(quota["monthly_minute_budget"])
    except (TypeError, ValueError) as exc:
        raise ValueError("monthly_minute_budget must be a number of minutes") from exc
    if not math.isfinite(budget) or budget < 0:
        raise ValueError("monthly_minute_budget must be a finite number of minutes, not negative")
    return budget


def _read_minutes_per_run(quota: Mapping[str, Any]) -> float:
    """What one metered dispatch is assumed to cost.

    A hardcoded rate would be a number nobody can correct: the ceiling holds only
    if the assumption is right, and the assumption is the one part of it an
    operator can actually get wrong. Real runs either cost more than assumed, in
    which case the month is spent before the count says so, or less, in which case
    dispatches are refused while allowance remains. Both are invisible, so the
    rate is policy and not a constant in the arithmetic.
    """
    if "minutes_per_run" not in quota:
        return DEFAULT_MINUTES_PER_RUN
    try:
        rate = float(quota["minutes_per_run"])
    except (TypeError, ValueError) as exc:
        raise ValueError("minutes_per_run must be a number of minutes") from exc
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("minutes_per_run must be a positive number of minutes")
    return rate


class DispatchNotPermitted(ValueError):
    """Raised when a dispatch target is outside the policy allowlist."""


@dataclass(frozen=True)
class Policy:
    hosted_runner: tuple[str, ...]
    local_priority: tuple[str, ...]
    device_priority: tuple[str, ...]
    lane_labels: Mapping[str, tuple[str, ...]]
    minimum_remaining_minutes: float
    reserve_minutes: float
    paid_overage: str
    unknown_quota: str
    dispatch_targets: tuple[DispatchTarget, ...] = ()
    quota_source: QuotaSource | None = None
    minute_budget: float | None = None  # a metered ceiling, absent means none
    minutes_per_run: float = DEFAULT_MINUTES_PER_RUN
    metered_minutes: Callable[[], float] | None = None  # set by attach_budget
    #: what one dispatch is assumed to cost, the rate the ceiling is built on.
    #: The check will not admit a dispatch for less than this, whatever a caller
    #: says it expects to spend.
    assumed_minutes_per_run: float = 0.0
    #: takes a hold on the budget for a dispatch, or None when there is nothing
    #: to hold against. set by attach_budget, and the reason the check and the
    #: hold can be one step
    reserve_metered_minutes: Callable[[float], int] | None = None
    release_metered_minutes: Callable[[int | None], None] | None = None
    #: held by whatever the check reads and the hold writes, so two simultaneous
    #: dispatches cannot both pass the same remaining budget
    #: A digest of the policy this object was built from.
    #:
    #: Taken at load and never re-read, because the question it answers is "is the
    #: broker enforcing the policy I think it is". Re-reading the file would answer
    #: it with the file's current contents, which is the exact disagreement this
    #: exists to expose: a ConfigMap updated under a running process reports the new
    #: policy's digest while enforcing the old one, which is the same silence with
    #: a fingerprint attached.
    fingerprint: str = ""

    budget_lock: threading.Lock = dataclasses_field(default_factory=threading.Lock)

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        raw = Path(path).read_bytes()
        data = tomllib.loads(raw.decode("utf-8"))
        hosted = data.get("hosted", {})
        quota = data.get("quota", {})
        local = data.get("local", {})
        lanes = data.get("lanes", {})
        return cls(
            hosted_runner=tuple([str(hosted.get("runner", "ubuntu-24.04"))]),
            local_priority=tuple(str(item) for item in local.get("priority", ["fallback", "device"])),
            device_priority=tuple(str(item) for item in local.get("device_priority", ["desktop", "laptop", "phone"])),
            lane_labels={
                str(name): tuple(str(label) for label in config.get("labels", []))
                for name, config in lanes.items()
            },
            minimum_remaining_minutes=float(quota.get("minimum_remaining_minutes", 20)),
            reserve_minutes=float(quota.get("reserve_minutes", 10)),
            paid_overage=str(quota.get("paid_overage", "deny")),
            unknown_quota=str(quota.get("unknown_quota", "local")),
            dispatch_targets=load_dispatch_targets(data),
            minute_budget=_read_minute_budget(quota),
            minutes_per_run=_read_minutes_per_run(quota),
            fingerprint=hashlib.sha256(raw).hexdigest()[:16],
        )

    def target_for(self, repository: str) -> DispatchTarget | None:
        """The entry that governs a repository, or None when policy names none.

        An exact entry wins over an owner default, so a repository the policy
        names explicitly is never governed by a broader default, even when that
        default would have permitted more.
        """
        lowered = repository.lower()
        exact = next(
            (entry for entry in self.dispatch_targets if entry.repository.lower() == lowered),
            None,
        )
        if exact is not None:
            return exact
        owner = lowered.split("/", 1)[0]
        return next(
            (
                entry
                for entry in self.dispatch_targets
                if entry.repository.lower() == f"{owner}/*"
            ),
            None,
        )

    def check_dispatch(self, repository: str, workflow: str, ref: str) -> None:
        """Refuse a dispatch target the policy does not name.

        Callers run this before any GitHub request, so a target outside the
        allowlist never reaches the API. The message names the refused dimension
        and never a configured value, so a refused request cannot be used to read
        the allowlist back out.
        """
        # GitHub resolves a repository name case-insensitively, so an exact
        # comparison would refuse a valid caller for its casing and report it as
        # a permissions problem rather than a typo
        target = self.target_for(repository)
        if target is None:
            raise DispatchNotPermitted("repository is not permitted for dispatch")
        if not target.dispatches:
            raise DispatchNotPermitted("repository is not permitted for dispatch")
        if workflow not in target.workflows:
            raise DispatchNotPermitted("workflow is not permitted for this repository")
        if not _ref_permitted(canonical_ref(ref), target.refs):
            raise DispatchNotPermitted("ref is not permitted for this workflow")

    def push_lane(self, repository: str) -> str | None:
        """The lane a push tries first, or None when the device path is closed.

        `"hosted"` is the default because GitHub's own runners cost this account
        nothing, and `"device"` is the opt-in for a repository whose maintainer
        wants its pushes on local hardware instead. Either way the other lane is
        the fallback, so this names the preference and not the only option.
        """
        target = self.target_for(repository)
        if target is None or not target.executes_on_device:
            return None
        if not target.dispatches:
            # A device-only repository names no dispatch workflow, so preferring
            # the hosted lane would send its push somewhere the policy has not
            # agreed can serve it, and the fallback would arrive after a refusal
            # the caller has already been told about.
            return "device"
        return target.push_lane

    def permits_device(self, repository: str) -> bool:
        """Whether a repository's trusted events may run on a device.

        The device path's own trust gate is the event's ``direct_eligible``, not
        this list, so answering true here grants no more than naming the
        repository. Matching is case-insensitive for the reason ``check_dispatch``
        matches that way: a delivery is refused for its casing otherwise.
        """
        target = self.target_for(repository)
        return target is not None and target.executes_on_device

    def choose(self, request: RouteRequest, runners: tuple[Runner, ...]) -> RouteDecision:
        reading = self._quota_reading(request)
        if reading is not None and reading.status != QUOTA_UNKNOWN:
            request = replace(
                request,
                quota_status=reading.status,
                remaining_minutes=reading.remaining_minutes,
            )
        decision = self._choose(request, runners)
        if reading is None:
            return decision
        # Nothing is appended here. The lane reason from _choose already says what
        # a reader needs, in the words a reader needs: hosted quota is available,
        # unknown, or exhausted. What used to be appended named the billing
        # endpoint, the failure reason and the age of the reading, and this reason
        # reaches /v1/route, which is deliberately unauthenticated. That made an
        # open preview endpoint a free oracle for one organization's Actions
        # allowance: whether it is spent, whether billing is reachable, and how this
        # policy's thresholds compare.
        #
        # The detail belongs in /v1/pool, which is token-gated and is where an
        # operator looks when they want to know why hosted was not chosen.
        return decision

    def _quota_clock(self) -> float:
        """The source's clock, so a reading is aged by the clock that took it."""
        now = getattr(self.quota_source, "now", None)
        return float(now()) if callable(now) else time.time()

    def _quota_reading(self, request: RouteRequest) -> QuotaReading | None:
        """The hosted reading that may replace a caller's unstated quota.

        Only a request that left quota unknown is upgraded, so figures a caller
        states stay authoritative, and an explicit lane never consults billing.
        A reading the source will not serve (never read, stale, unauthorized)
        keeps the unknown path exactly as configured.
        """
        if self.quota_source is None:
            return None
        if request.requested_lane != "auto":
            return None
        if request.repository_visibility != "private":
            return None
        if request.quota_status != "unknown" or request.remaining_minutes is not None:
            return None
        return self.quota_source.current()

    def _choose(self, request: RouteRequest, runners: tuple[Runner, ...]) -> RouteDecision:
        # the check and the hold are one step, so a second request arriving at the
        # same moment sees this one's hold rather than the same remaining budget
        reservation, refusal = reserve_metered(self, request.estimated_minutes)
        if refusal is not None:
            return self._first_local(request, runners, refusal)
        try:
            decision = self._choose_lane(request, runners)
        except Exception:
            # a request that cannot be routed at all has not spent anything, and
            # a hold left behind would tighten the month for every request after
            release_hold(self, reservation)
            raise
        if reservation is not None and decision.lane == HOSTED_LANE:
            return replace(decision, reservation=reservation)
        if reservation is not None:
            # a hold taken for a metered lane that the decision did not use has to
            # go back, or the ceiling is charged for a dispatch nobody made
            release_hold(self, reservation)
        return decision

    def _choose_lane(self, request: RouteRequest, runners: tuple[Runner, ...]) -> RouteDecision:
        if request.requested_lane == "hosted":
            return self._hosted("hosted lane explicitly selected")
        if request.requested_lane != "auto":
            return self._explicit_lane(request, runners)
        if request.repository_visibility == "public":
            return self._hosted("public repository uses hosted runners")
        if self._hosted_quota_available(request):
            return self._hosted("hosted quota is available")
        if request.quota_status == "unknown":
            reason = "hosted quota is unknown"
        elif request.quota_status == "exhausted":
            # a spent allowance reads as exhausted even when remaining_minutes
            # is zero, so the status decides, not the figure
            reason = "hosted quota is exhausted"
        else:
            # available but too thin for this job, which is not the same as the
            # allowance being gone, and the reason is user-visible
            reason = (
                f"hosted quota has {request.remaining_minutes:g} minutes left,"
                f" too few for a {request.estimated_minutes or 0:g} minute job"
            )
        return self._first_local(request, runners, reason)

    def _hosted_quota_available(self, request: RouteRequest) -> bool:
        if request.quota_status in {"available", "paid_allowed"}:
            if request.remaining_minutes is None:
                return True
            return request.remaining_minutes - request.estimated_minutes - self.reserve_minutes >= self.minimum_remaining_minutes
        if request.quota_status == "unknown":
            return self.unknown_quota == "hosted"
        if request.quota_status == "exhausted":
            return self.paid_overage == "allow"
        if request.remaining_minutes is None:
            return False
        return request.remaining_minutes - request.estimated_minutes - self.reserve_minutes >= self.minimum_remaining_minutes

    def _hosted(self, reason: str) -> RouteDecision:
        return RouteDecision(lane="hosted", runs_on=self.hosted_runner, reason=reason)

    def _explicit_lane(self, request: RouteRequest, runners: tuple[Runner, ...]) -> RouteDecision:
        lane = request.requested_lane
        labels = self.lane_labels.get(lane)
        if labels is None:
            raise RouteUnavailable(f"unknown lane: {lane}")
        if lane == "device":
            if not request.target:
                raise RouteUnavailable("device lane requires an explicit target")
            if request.target not in self.device_priority:
                raise RouteUnavailable(f"unknown device target: {request.target}")
        required = set(labels)
        if lane == "device" and request.target:
            required.add(request.target)
        runner = self._first_eligible(runners, required)
        if runner is None:
            raise RouteUnavailable(f"no eligible runner for lane {lane}")
        # Emit the lane's own labels, not the labels of whichever runner the
        # static state picked. GitHub scopes self-hosted runners to a single
        # repository, so a runner name from inventory does not exist in most
        # repositories: the same runner is registered under a different name in
        # each repository. Pinning runs_on to a runner name makes the job queue
        # forever. The broker decides the lane; GitHub picks the runner
        # registered to the repository.
        return RouteDecision(lane=lane, runs_on=tuple(required), reason=f"explicit {lane} lane selected", runner=runner.name)

    def _first_local(self, request: RouteRequest, runners: tuple[Runner, ...], reason: str) -> RouteDecision:
        for lane in self.local_priority:
            if lane == "device":
                continue
            try:
                decision = self._explicit_lane(
                    RouteRequest(requested_lane=lane, target=request.target),
                    runners,
                )
                return RouteDecision(
                    lane=decision.lane,
                    runs_on=decision.runs_on,
                    reason=f"{reason}; selected {lane}",
                    runner=decision.runner,
                )
            except RouteUnavailable:
                continue
        # the reason travels with the refusal. A caller that has nowhere to fall
        # back to is exactly the caller who needs to be told whether the cause is
        # a spent ceiling or an absent allowance, and a single fixed sentence
        # answers neither.
        raise RouteUnavailable(f"{reason}; no local runner is eligible")

    def _first_eligible(self, runners: tuple[Runner, ...], required: set[str]) -> Runner | None:
        eligible = [
            runner
            for runner in runners
            if runner.healthy and runner.status == "online" and not runner.busy and required.issubset(runner.labels)
        ]
        if not eligible:
            return None
        return sorted(eligible, key=lambda runner: runner.name)[0]


def canonical_ref(ref: str) -> str:
    """Name a ref the way git does, so a branch compares against a full ref.

    A short name is read as a branch. A tag has to carry its ``refs/tags/``
    prefix, which leaves tag and pull-request refs out unless policy names them.
    """
    return ref if ref.startswith("refs/") else BRANCH_REF_ROOT + ref
