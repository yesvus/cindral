import unittest

from runner_relay.models import RouteRequest, Runner
from runner_relay.policy import Policy, RouteUnavailable


class PolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = Policy(
            hosted_runner=("ubuntu-24.04",),
            local_priority=("fallback", "device"),
            device_priority=("papyrus", "rover", "colak"),
            lane_labels={
                "fallback": ("self-hosted", "Linux", "ARM64", "fallback"),
                "device": ("self-hosted", "Linux", "ARM64", "device"),
                "burst": ("self-hosted", "Linux", "ARM64", "burst"),
            },
            minimum_remaining_minutes=20,
            reserve_minutes=10,
            paid_overage="deny",
        )
        self.runners = (
            Runner("gurbet", "online", False, ("self-hosted", "Linux", "ARM64", "fallback")),
            Runner("papyrus", "online", False, ("self-hosted", "Linux", "ARM64", "papyrus", "device")),
            Runner("zombie-burst", "online", False, ("self-hosted", "Linux", "ARM64", "burst")),
        )

    def test_available_quota_prefers_hosted(self) -> None:
        decision = self.policy.choose(
            RouteRequest(quota_status="available", remaining_minutes=100),
            self.runners,
        )
        self.assertEqual(decision.lane, "hosted")
        self.assertEqual(decision.runs_on, ("ubuntu-24.04",))

    def test_public_repository_prefers_hosted(self) -> None:
        decision = self.policy.choose(
            RouteRequest(repository_visibility="public", quota_status="exhausted"),
            self.runners,
        )
        self.assertEqual(decision.lane, "hosted")

    def test_exhausted_quota_prefers_fallback(self) -> None:
        decision = self.policy.choose(RouteRequest(quota_status="exhausted"), self.runners)
        self.assertEqual(decision.lane, "fallback")
        self.assertEqual(decision.runner, "gurbet")

    def test_explicit_burst_selects_burst_runner(self) -> None:
        decision = self.policy.choose(RouteRequest(requested_lane="burst"), self.runners)
        self.assertEqual(decision.runner, "zombie-burst")

    def test_explicit_device_requires_target(self) -> None:
        decision = self.policy.choose(
            RouteRequest(requested_lane="device", target="papyrus"),
            self.runners,
        )
        self.assertEqual(decision.runner, "papyrus")

    def test_unavailable_route_fails_before_job(self) -> None:
        with self.assertRaises(RouteUnavailable):
            self.policy.choose(
                RouteRequest(requested_lane="device", target="colak"),
                self.runners,
            )


if __name__ == "__main__":
    unittest.main()
