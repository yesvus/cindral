"""Deterministic runner selection policy."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
import tomllib

from .models import RouteDecision, RouteRequest, Runner


class RouteUnavailable(ValueError):
    """Raised when policy cannot select an eligible runner."""


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

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        with Path(path).open("rb") as stream:
            data = tomllib.load(stream)
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
        )

    def choose(self, request: RouteRequest, runners: tuple[Runner, ...]) -> RouteDecision:
        if request.requested_lane == "hosted":
            return self._hosted("hosted lane explicitly selected")
        if request.requested_lane != "auto":
            return self._explicit_lane(request, runners)
        if request.repository_visibility == "public":
            return self._hosted("public repository uses hosted runners")
        if self._hosted_quota_available(request):
            return self._hosted("hosted quota is available")
        reason = "hosted quota is unknown" if request.quota_status == "unknown" else "hosted quota is exhausted"
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
        required = set(labels)
        if lane == "device" and request.target:
            required.add(request.target)
        runner = self._first_eligible(runners, required)
        if runner is None:
            raise RouteUnavailable(f"no eligible runner for lane {lane}")
        return RouteDecision(lane=lane, runs_on=runner.labels, reason=f"explicit {lane} lane selected", runner=runner.name)

    def _first_local(self, request: RouteRequest, runners: tuple[Runner, ...], reason: str) -> RouteDecision:
        for lane in self.local_priority:
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
        raise RouteUnavailable("hosted quota unavailable and no local runner is eligible")

    def _first_eligible(self, runners: tuple[Runner, ...], required: set[str]) -> Runner | None:
        eligible = [
            runner
            for runner in runners
            if runner.healthy and runner.status == "online" and not runner.busy and required.issubset(runner.labels)
        ]
        if not eligible:
            return None
        return sorted(eligible, key=lambda runner: runner.name)[0]
