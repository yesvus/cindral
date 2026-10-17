"""Data models for routing requests, dispatch targets, and runner state."""
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int
    log: str = ""
    # parsed from the device's complete log, because the reported log is a
    # bounded tail and would otherwise drop the steps that ran first
    steps: list[dict] = field(default_factory=list)
    # where the run left its artifact archive, for the device to upload before
    # it reports. Empty when the run produced no artifact directory, which is
    # most runs.
    artifacts: str = ""


@dataclass(frozen=True)
class Runner:
    name: str
    status: str
    busy: bool
    labels: tuple[str, ...]
    healthy: bool = True

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Runner":
        return cls(
            name=str(value["name"]),
            status=str(value.get("status", "offline")),
            busy=bool(value.get("busy", False)),
            labels=tuple(str(label) for label in value.get("labels", [])),
            healthy=bool(value.get("healthy", True)),
        )


@dataclass(frozen=True)
class RouteRequest:
    requested_lane: str = "auto"
    target: str | None = None
    repository_visibility: str = "private"
    quota_status: str = "unknown"
    remaining_minutes: float | None = None
    estimated_minutes: float = 10

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RouteRequest":
        remaining = value.get("remaining_minutes")
        target = value.get("target")
        if target is not None and not isinstance(target, str):
            raise ValueError("target must be a string")
        return cls(
            requested_lane=str(value.get("requested_lane", "auto")),
            target=target,
            repository_visibility=str(value.get("repository_visibility", "private")),
            quota_status=str(value.get("quota_status", "unknown")),
            remaining_minutes=float(remaining) if remaining is not None else None,
            estimated_minutes=float(value.get("estimated_minutes", 10)),
        )


@dataclass(frozen=True)
class DispatchTarget:
    """One repository the broker serves, and which paths it may serve it on.

    ``mode`` is required rather than defaulted, so what a repository is allowed
    is one readable line instead of an implicit both. ``workflows`` and ``refs``
    constrain the dispatch path and are exact names, with an optional trailing
    ``*`` on a ref naming a branch-name prefix. An empty list matches nothing, so
    a half-configured target cannot dispatch.
    """

    mode: str
    repository: str
    workflows: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    # which lane a push tries first, for a repository that may be served here.
    # "hosted" is GitHub's own runners, which cost a user account nothing until
    # its included minutes run out; "device" is the local pool. The other lane
    # is the fallback, so this is a preference and not an exclusion. It is one
    # field rather than a second `mode` because `mode` says which paths may
    # serve a repository at all.
    push_lane: str = "hosted"

    @property
    def dispatches(self) -> bool:
        return self.mode in {"dispatch", "both"}

    @property
    def executes_on_device(self) -> bool:
        return self.mode in {"device", "both"}


@dataclass(frozen=True)
class RouteDecision:
    lane: str
    runs_on: tuple[str, ...]
    reason: str
    runner: str | None = None
    capacity: dict[str, Any] | None = None
    #: the metered hold taken for this dispatch, released if it does not go. Not
    #: user-facing, and absent from `as_dict` because it is broker bookkeeping.
    reservation: int | None = None

    def as_dict(self) -> dict[str, Any]:
        result = {
            "lane": self.lane,
            "runs_on": list(self.runs_on),
            "reason": self.reason,
            "runner": self.runner,
        }
        if self.capacity is not None:
            result["capacity"] = self.capacity
        return result
