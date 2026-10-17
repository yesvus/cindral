"""Select webhook dispatches that GitHub can queue on busy online runners."""
from collections.abc import Iterable
from dataclasses import replace

from .github import RepositoryRunner
from .models import RouteDecision, RouteRequest, Runner
from .policy import Policy, RouteUnavailable


def overlapping_reservations(
    repository_runners: tuple[RepositoryRunner, ...],
    required_labels: set[str],
    reservations: Iterable[tuple[float, tuple[str, ...]]],
) -> int:
    idle_labels = [
        set(runner.labels) for runner in repository_runners
        if runner.status == "online" and not runner.busy
    ]
    return sum(
        any(set(labels).union(required_labels).issubset(runner_labels) for runner_labels in idle_labels)
        for _, labels in reservations
    )


def choose_webhook_lane(
    policy: Policy, request: RouteRequest, runners: tuple[Runner, ...], allow_busy: bool
) -> RouteDecision:
    try:
        return policy.choose(request, runners)
    except RouteUnavailable:
        if not allow_busy:
            raise
        online = tuple(replace(runner, busy=False) for runner in runners if runner.status == "online")
        decision = policy.choose(request, online)
        return replace(decision, reason=f"{decision.reason}; GitHub queues until the runner is idle")
