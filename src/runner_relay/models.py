"""Data models for routing requests and runner state."""
from dataclasses import dataclass
from typing import Any, Mapping


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
class RouteDecision:
    lane: str
    runs_on: tuple[str, ...]
    reason: str
    runner: str | None = None
    capacity: dict[str, Any] | None = None

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
