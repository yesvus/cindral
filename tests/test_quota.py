"""Tests for the caching quota source and its wiring into routing.

The readings the source caches are parsed in billing.py and covered by
test_billing.py; this covers what the cache does with them.
"""
import threading
import json
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from cindral.billing import (
    QUOTA_AVAILABLE,
    QUOTA_EXHAUSTED,
    QUOTA_UNKNOWN,
    SOURCE_ACTIONS_BILLING,
    QuotaFailure,
    QuotaReading,
    bounded_seconds,
)
from cindral.models import RouteRequest, Runner
from cindral.policy import Policy
from cindral.quota import SOURCE_NONE, QuotaSource, configured_source
from cindral.service import attach_quota_source

SEPTEMBER = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).timestamp()
TOKEN = "secret-token-value"
class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class ScriptedClient:
    """Serves scripted outcomes: a string fails the read, a reading succeeds."""

    def __init__(self, *outcomes, default: QuotaReading | None = None) -> None:
        self.outcomes = list(outcomes)
        self.default = default
        self.reads = 0

    def read(self, organization: str, timeout: float | None = None) -> QuotaReading:
        self.reads += 1
        outcome = self.outcomes.pop(0) if self.outcomes else self.default
        if isinstance(outcome, str):
            raise QuotaFailure(outcome)
        if outcome is None:
            raise RuntimeError("no scripted reading")
        return outcome


def reading(status: str, observed_at: float, **fields) -> QuotaReading:
    return QuotaReading(status=status, source=SOURCE_ACTIONS_BILLING, observed_at=observed_at, **fields)



class QuotaSourceTest(unittest.TestCase):
    def source(self, client, clock=None, **kwargs) -> QuotaSource:
        notes: list[str] = []
        clock = clock or Clock(SEPTEMBER)
        source = QuotaSource(
            client,
            "example-org",
            clock=clock,
            note=notes.append,
            **kwargs,
        )
        source.notes = notes
        return source

    def test_stop_reports_a_refresh_that_outlasted_the_join(self) -> None:
        # a refresh in flight can outlast the join, and clearing the handle
        # anyway let a later start spawn a second refresh thread
        started = threading.Event()
        blocked = threading.Event()

        class BlockingClient(ScriptedClient):
            def read(self, organization: str, timeout: float | None = None) -> QuotaReading:
                started.set()
                blocked.wait(5)
                return reading(QUOTA_AVAILABLE, SEPTEMBER, period="2026-09")

        source = self.source(BlockingClient(), refresh_interval=0.01, max_age=900.0)
        source.start()
        self.assertTrue(started.wait(5))
        source.stop(timeout=0.2)
        source.start()  # the handle is still held, so this cannot double up
        blocked.set()
        source.stop(timeout=5)
        self.assertEqual([n for n in source.notes if "did not stop" in n], [
            "warning: hosted quota refresh did not stop within the shutdown timeout"
        ])

    def test_a_finished_thread_does_not_keep_the_source_from_refreshing(self) -> None:
        # a handle left behind by a stop whose join timed out became a permanent
        # no-op on start, so the source degraded to stale and then to unknown
        # forever with no refresh thread running
        blocked = threading.Event()
        finished = threading.Event()
        started = threading.Event()

        class SlowClient(ScriptedClient):
            def read(self, organization: str, timeout: float | None = None) -> QuotaReading:
                started.set()
                try:
                    blocked.wait(5)
                finally:
                    finished.set()
                return super().read(organization, timeout)

        source = self.source(SlowClient(), refresh_interval=0.01, max_age=900.0)
        source.start()
        self.assertTrue(started.wait(5))
        source.stop(timeout=0.2)  # times out, so the handle is kept
        blocked.set()
        self.assertTrue(finished.wait(5))
        # the handle is still set, and start() must not treat a dead handle as a
        # running refresh
        source.start()
        try:
            self.assertIsNotNone(source._thread)
            self.assertTrue(source._thread.is_alive())
        finally:
            blocked.set()
            source.stop(timeout=5)

    def test_current_reports_unknown_never_zero_before_any_refresh(self) -> None:
        source = self.source(ScriptedClient(default=reading(QUOTA_AVAILABLE, SEPTEMBER)))
        current = source.current()
        self.assertEqual(current.status, QUOTA_UNKNOWN)
        self.assertEqual(current.source, SOURCE_NONE)
        self.assertIsNone(current.remaining_minutes)
        self.assertIsNone(current.included_minutes)
        self.assertIsNone(current.used_minutes)

    def test_refresh_stores_the_good_reading(self) -> None:
        source = self.source(
            ScriptedClient(default=reading(QUOTA_AVAILABLE, SEPTEMBER, remaining_minutes=2695.0))
        )
        source.refresh()
        current = source.current()
        self.assertEqual(current.status, QUOTA_AVAILABLE)
        self.assertEqual(current.remaining_minutes, 2695.0)
        self.assertEqual(current.source, SOURCE_ACTIONS_BILLING)

    def test_failed_refresh_keeps_the_last_good_reading(self) -> None:
        good = reading(QUOTA_AVAILABLE, SEPTEMBER, remaining_minutes=2695.0)
        source = self.source(ScriptedClient(good, "unreachable", default=good))
        source.refresh()
        self.assertIs(source.current(), good)
        source.refresh()
        self.assertIs(source.current(), good)

    def test_failure_reason_is_reported_while_no_good_reading_exists(self) -> None:
        source = self.source(ScriptedClient("unauthorized"))
        source.refresh()
        current = source.current()
        self.assertEqual(current.status, QUOTA_UNKNOWN)
        self.assertEqual(current.reason, "unauthorized")
        self.assertIsNone(current.remaining_minutes)

    def test_reading_older_than_max_age_reads_unknown(self) -> None:
        clock = Clock(SEPTEMBER)
        source = self.source(
            ScriptedClient(default=reading(QUOTA_AVAILABLE, SEPTEMBER, remaining_minutes=2695.0)),
            clock=clock,
            max_age=900,
        )
        source.refresh()
        clock.now = SEPTEMBER + 901
        current = source.current()
        self.assertEqual(current.status, QUOTA_UNKNOWN)
        self.assertEqual(current.reason, "stale")
        self.assertIsNone(current.remaining_minutes)

    def test_failure_note_prints_once_per_state_change(self) -> None:
        good = reading(QUOTA_AVAILABLE, SEPTEMBER, remaining_minutes=2695.0)
        source = self.source(
            ScriptedClient("unreachable", "unreachable", good, "unreachable", default=good),
            max_age=900,
        )
        source.refresh()
        source.refresh()
        self.assertEqual(len(source.notes), 1)
        source.refresh()
        self.assertEqual(len(source.notes), 2)
        self.assertIn("recovered", source.notes[1])
        source.refresh()
        self.assertEqual(len(source.notes), 3)
        self.assertIn("unreachable", source.notes[2])
        self.assertNotIn(TOKEN, " ".join(source.notes))

    def test_refresh_loop_runs_on_the_interval_and_stops(self) -> None:
        class CountingClient:
            def __init__(self) -> None:
                self.count = 0
                self.lock = threading.Lock()

            def read(self, organization: str, timeout: float | None = None) -> QuotaReading:
                with self.lock:
                    self.count += 1
                return QuotaReading(status=QUOTA_AVAILABLE, source=SOURCE_ACTIONS_BILLING, observed_at=time.time())

        client = CountingClient()
        source = QuotaSource(client, "example-org", refresh_interval=0.02, max_age=900, note=lambda message: None)
        source.start()
        deadline = time.time() + 2
        while client.count < 3 and time.time() < deadline:
            time.sleep(0.005)
        source.stop()
        self.assertGreaterEqual(client.count, 3)
        settled = client.count
        time.sleep(0.1)
        self.assertEqual(client.count, settled)

    def test_unexpected_client_errors_are_treated_as_unreachable(self) -> None:
        class BrokenClient:
            def read(self, organization: str, timeout: float | None = None) -> QuotaReading:
                raise RuntimeError("bug")

        source = self.source(BrokenClient())
        source.refresh()
        self.assertEqual(source.current().reason, "unreachable")

    def test_configuration_is_refused_without_an_organization(self) -> None:
        with self.assertRaises(ValueError):
            QuotaSource(ScriptedClient(), "")


class FakeSource:
    def __init__(self, reading: QuotaReading, clock: float | None = None) -> None:
        self.reading = reading
        self.consulted = 0
        self._instant = clock if clock is not None else self.reading.observed_at
        # deliberately wrong, so reaching for the private attribute instead of
        # the public one is visible in the reported age
        self._clock = lambda: float("nan")

    def now(self) -> float:
        return self._instant

    def current(self) -> QuotaReading:
        self.consulted += 1
        return self.reading


class PolicyQuotaTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runners = (
            Runner("server", "online", False, ("self-hosted", "Linux", "ARM64", "fallback")),
            Runner("desktop", "online", False, ("self-hosted", "Linux", "ARM64", "desktop", "device")),
        )

    @staticmethod
    def built_policy(**overrides) -> Policy:
        base = dict(
            hosted_runner=("ubuntu-24.04",),
            local_priority=("fallback", "device"),
            device_priority=("desktop", "laptop", "phone"),
            lane_labels={
                "fallback": ("self-hosted", "Linux", "ARM64", "fallback"),
                "device": ("self-hosted", "Linux", "ARM64", "device"),
                "burst": ("self-hosted", "Linux", "ARM64", "burst"),
            },
            minimum_remaining_minutes=20,
            reserve_minutes=10,
            paid_overage="deny",
            unknown_quota="local",
        )
        base.update(overrides)
        return Policy(**base)

    def private_request(self, **overrides) -> RouteRequest:
        fields = dict(requested_lane="auto", repository_visibility="private", quota_status="unknown")
        fields.update(overrides)
        return RouteRequest(**fields)

    def test_a_spent_allowance_says_exhausted_not_too_thin(self) -> None:
        # a reading-sourced exhausted status carries remaining_minutes of zero,
        # so branching on the figure described a spent allowance as a thin one,
        # on the commit status a reader sees
        spent = QuotaReading(
            status=QUOTA_EXHAUSTED,
            source=SOURCE_ACTIONS_BILLING,
            observed_at=time.time(),
            included_minutes=3000.0,
            used_minutes=3200.0,
            remaining_minutes=0.0,
        )
        reason = self.built_policy(quota_source=FakeSource(spent)).choose(
            self.private_request(), self.runners
        ).reason
        self.assertIn("hosted quota is exhausted", reason)
        self.assertNotIn("too few", reason)

    def test_an_available_but_thin_allowance_names_the_minutes(self) -> None:
        thin = QuotaReading(
            status=QUOTA_AVAILABLE,
            source=SOURCE_ACTIONS_BILLING,
            observed_at=time.time(),
            included_minutes=3000.0,
            used_minutes=2985.0,
            remaining_minutes=15.0,
        )
        reason = self.built_policy(quota_source=FakeSource(thin)).choose(
            self.private_request(), self.runners
        ).reason
        self.assertIn("has 15 minutes left", reason)
        self.assertNotIn("exhausted", reason)

    def test_an_unknown_reading_says_so_without_naming_anything_billing(self) -> None:
        """The reason reaches an unauthenticated endpoint, so it carries no detail.

        This used to name the endpoint that could not answer, on the reasoning that
        an operator reading a commit status needs to know which of the two billing
        sources is broken. But the same string comes back from `POST /v1/route`,
        which is deliberately unauthenticated, so it made an open endpoint a free
        oracle for one organization's Actions allowance: whether it is spent,
        whether billing is reachable, and which of the two endpoints this broker
        talks to.

        Which of the two is broken is an operator question, and `/v1/pool` is
        token-gated and answers it.
        """
        unreadable = QuotaReading(status=QUOTA_UNKNOWN, source="actions_billing", observed_at=time.time())
        reason = self.built_policy(quota_source=FakeSource(unreadable)).choose(
            self.private_request(), self.runners
        ).reason
        self.assertIn("hosted quota is unknown", reason)
        for leaked in ("actions_billing", "billing_usage", "unauthorized", "unreachable", "malformed"):
            self.assertNotIn(leaked, reason, f"the route reason leaks {leaked}")

    def test_a_route_reason_carries_no_organization_and_no_repository(self) -> None:
        """The test that catches a future field being composed into the string.

        Asserted against the response rather than the reason alone, because the
        decision is serialised and a field can reach the payload without reaching
        the sentence.
        """
        reading = QuotaReading(
            status=QUOTA_AVAILABLE,
            source=SOURCE_ACTIONS_BILLING,
            observed_at=time.time() - 41,
            included_minutes=3000.0,
            used_minutes=305.0,
            remaining_minutes=2695.0,
        )
        decision = self.built_policy(quota_source=FakeSource(reading, clock=time.time())).choose(
            self.private_request(), self.runners
        )
        serialised = json.dumps(decision.as_dict())
        self.assertNotIn("actions_billing", serialised)
        self.assertNotIn("organization", serialised)
        self.assertNotIn("repositoryName", serialised)

    def test_the_decision_ages_the_reading_with_the_source_clock(self) -> None:
        """A reading is aged by the source's clock, and the age still decides.

        The age used to appear in the reason as well. It does not now, because the
        reason reaches an unauthenticated endpoint, but the ageing itself is
        unchanged: this pins the decision, not the sentence.
        """
        source = FakeSource(
            QuotaReading(
                status=QUOTA_AVAILABLE,
                source=SOURCE_ACTIONS_BILLING,
                observed_at=1_000_000.0,
                included_minutes=3000.0,
                used_minutes=100.0,
                remaining_minutes=2900.0,
            ),
            clock=1_000_041.0,
        )
        decision = self.built_policy(quota_source=source).choose(self.private_request(), self.runners)
        self.assertEqual(decision.lane, "hosted")
        self.assertNotIn("41s", decision.reason)
        self.assertNotIn(SOURCE_ACTIONS_BILLING, decision.reason)

    def test_available_reading_routes_hosted_without_recording_source_or_age(self) -> None:
        good = QuotaReading(
            status=QUOTA_AVAILABLE,
            source=SOURCE_ACTIONS_BILLING,
            observed_at=time.time() - 41,
            included_minutes=3000.0,
            used_minutes=305.0,
            remaining_minutes=2695.0,
        )
        source = FakeSource(good, clock=time.time())
        decision = self.built_policy(quota_source=source).choose(self.private_request(), self.runners)
        self.assertEqual(decision.lane, "hosted")
        self.assertIn("hosted quota is available", decision.reason)
        self.assertNotIn("actions_billing", decision.reason)
        self.assertNotIn("41s", decision.reason)

    def test_exhausted_reading_follows_the_paid_overage_setting(self) -> None:
        # only the legacy endpoint reports an allowance, so only it can be
        # exhausted; the enhanced report states no status at all
        good = QuotaReading(
            status=QUOTA_EXHAUSTED,
            source=SOURCE_ACTIONS_BILLING,
            observed_at=time.time(),
            included_minutes=3000.0,
            used_minutes=3200.0,
            remaining_minutes=0.0,
            paid_minutes=200.0,
        )
        decision = self.built_policy(quota_source=FakeSource(good, clock=time.time())).choose(
            self.private_request(), self.runners
        )
        self.assertEqual(decision.lane, "fallback")
        self.assertIn("hosted quota is exhausted", decision.reason)
        self.assertNotIn("actions_billing", decision.reason)
        allowed = self.built_policy(paid_overage="allow", quota_source=FakeSource(good))
        self.assertEqual(allowed.choose(self.private_request(), self.runners).lane, "hosted")

    def test_caller_supplied_quota_wins_over_the_reading(self) -> None:
        exhausted = QuotaReading(
            status=QUOTA_EXHAUSTED,
            source=SOURCE_ACTIONS_BILLING,
            observed_at=time.time(),
            remaining_minutes=0.0,
        )
        source = FakeSource(exhausted)
        decision = self.built_policy(quota_source=source).choose(
            self.private_request(quota_status="available", remaining_minutes=100),
            self.runners,
        )
        self.assertEqual(decision.lane, "hosted")
        self.assertNotIn("quota read", decision.reason)
        self.assertEqual(source.consulted, 0)
        decision = self.built_policy(quota_source=FakeSource(exhausted)).choose(
            self.private_request(remaining_minutes=100),
            self.runners,
        )
        self.assertEqual(decision.lane, "fallback")
        self.assertNotIn("quota read", decision.reason)

    def test_public_repositories_never_consult_billing(self) -> None:
        exhausted = QuotaReading(status=QUOTA_EXHAUSTED, source=SOURCE_ACTIONS_BILLING, observed_at=time.time())
        source = FakeSource(exhausted)
        decision = self.built_policy(quota_source=source).choose(
            RouteRequest(repository_visibility="public"),
            self.runners,
        )
        self.assertEqual(decision.lane, "hosted")
        self.assertEqual(source.consulted, 0)

    def test_explicit_lanes_never_consult_billing(self) -> None:
        exhausted = QuotaReading(status=QUOTA_EXHAUSTED, source=SOURCE_ACTIONS_BILLING, observed_at=time.time())
        source = FakeSource(exhausted)
        decision = self.built_policy(quota_source=source).choose(
            RouteRequest(requested_lane="hosted", repository_visibility="private"),
            self.runners,
        )
        self.assertEqual(decision.lane, "hosted")
        self.assertEqual(source.consulted, 0)
        self.assertNotIn("quota read", decision.reason)

    def test_unknown_reading_keeps_the_configured_unknown_behavior(self) -> None:
        unknown = QuotaReading(
            status=QUOTA_UNKNOWN,
            source=SOURCE_NONE,
            observed_at=time.time(),
            reason="unauthorized",
        )
        decision = self.built_policy(quota_source=FakeSource(unknown)).choose(self.private_request(), self.runners)
        self.assertEqual(decision.lane, "fallback")
        # the configured unknown behaviour still decides the lane. The reason says
        # quota is unknown and stops there: "unauthorized" is which of the two
        # billing endpoints is broken, which /v1/route has no business publishing.
        self.assertIn("hosted quota is unknown", decision.reason)
        self.assertNotIn("unauthorized", decision.reason)

    def test_no_source_keeps_the_unknown_path_exactly_as_before(self) -> None:
        decision = self.built_policy().choose(self.private_request(), self.runners)
        self.assertEqual(decision.lane, "fallback")
        self.assertEqual(decision.reason, "hosted quota is unknown; selected fallback")


class FakeBillingClient:
    def __init__(self, token: str, *args, **kwargs) -> None:
        self.token = token

    def read(self, organization: str, timeout: float | None = None) -> QuotaReading:
        return QuotaReading(
            status=QUOTA_AVAILABLE,
            source=SOURCE_ACTIONS_BILLING,
            observed_at=time.time(),
            included_minutes=3000.0,
            used_minutes=305.0,
            remaining_minutes=2695.0,
        )


class ConfiguredSourceTest(unittest.TestCase):
    def getenv(self, **values):
        return lambda name: values.get(name)

    def build(self, token: str | None = "token-value", getenv=None):
        notes: list[str] = []
        with patch("cindral.quota.BillingClient", FakeBillingClient):
            source = configured_source(token, getenv or self.getenv(CINDRAL_BILLING_ORG="example-org"), notes.append)
        return source, notes

    def test_named_organization_builds_a_refreshed_and_running_source(self) -> None:
        source, _ = self.build()
        try:
            self.assertEqual(source.current().status, QUOTA_AVAILABLE)
            self.assertEqual(source.current().source, SOURCE_ACTIONS_BILLING)
        finally:
            source.stop()

    def timeouts_over_http(self, getenv) -> list[float]:
        """The urlopen deadline of every real request a built source makes.

        The budget belongs to one read, not to the client, so a claim about it is
        only worth anything if it is read off the request that carries it.
        """
        import json as _json
        from io import BytesIO
        from cindral import billing

        seen: list[float] = []
        payload = _json.dumps({
            "total_minutes_used": 305, "total_paid_minutes_used": 0, "included_minutes": 3000,
        }).encode()

        def record(request, timeout=None):
            seen.append(float(timeout))
            return BytesIO(payload)

        notes: list[str] = []
        with patch.object(billing, "urlopen", side_effect=record):
            source = configured_source("token-value", getenv, notes.append)
            try:
                # the periodic refresh takes the client's own budget
                source.refresh()
            finally:
                source.stop()
        return seen

    def test_the_startup_read_gets_its_own_tighter_timeout(self) -> None:
        # the read taken before the broker listens is bounded tighter than the
        # periodic refresh, because three sequential requests at the refresh
        # timeout held startup for half a minute during an outage
        # the helper records the startup read and one periodic refresh, in order
        self.assertEqual(
            self.timeouts_over_http(self.getenv(CINDRAL_BILLING_ORG="example-org")), [3.0, 10.0]
        )
        self.assertEqual(
            self.timeouts_over_http(self.getenv(
                CINDRAL_BILLING_ORG="example-org",
                CINDRAL_BILLING_STARTUP_TIMEOUT_SECONDS="8",
            )),
            [8.0, 10.0],
        )

    def test_the_periodic_refresh_keeps_the_client_timeout(self) -> None:
        # a startup budget applied to the client would have shortened every
        # later refresh, so a billing API answering in the 3 to 10 second band
        # counted as unreachable on every cycle
        self.assertEqual(
            self.timeouts_over_http(self.getenv(
                CINDRAL_BILLING_ORG="example-org",
                CINDRAL_BILLING_STARTUP_TIMEOUT_SECONDS="2",
            )),
            [2.0, 10.0],
        )

    def test_the_startup_timeout_is_bounded(self) -> None:
        env = self.getenv(
            CINDRAL_BILLING_ORG="example-org",
            CINDRAL_BILLING_STARTUP_TIMEOUT_SECONDS="0",
        )
        with self.assertRaises(ValueError):
            self.build(getenv=env)

    def test_unnamed_organization_builds_nothing(self) -> None:
        source, notes = self.build(getenv=self.getenv())
        self.assertIsNone(source)
        self.assertEqual(notes, [])

    def test_organization_without_a_token_builds_nothing_and_notes(self) -> None:
        source, notes = self.build(token=None)
        self.assertIsNone(source)
        self.assertTrue(any("GitHub credential" in note for note in notes))

    def test_refresh_interval_is_bounded(self) -> None:
        env = self.getenv(CINDRAL_BILLING_ORG="example-org", CINDRAL_BILLING_REFRESH_SECONDS="10")
        with self.assertRaises(ValueError):
            self.build(getenv=env)
        env = self.getenv(CINDRAL_BILLING_ORG="example-org", CINDRAL_BILLING_REFRESH_SECONDS="abc")
        with self.assertRaises(ValueError):
            self.build(getenv=env)

    def test_max_age_must_not_expire_readings_before_the_next_refresh(self) -> None:
        env = self.getenv(CINDRAL_BILLING_ORG="example-org", CINDRAL_BILLING_MAX_AGE_SECONDS="30")
        with self.assertRaises(ValueError):
            self.build(getenv=env)

    def test_bounded_seconds_reads_defaults_and_refuses_outliers(self) -> None:
        self.assertEqual(bounded_seconds("", 300.0, 60.0, 3600.0, "NAME"), 300.0)
        self.assertEqual(bounded_seconds("120", 300.0, 60.0, 3600.0, "NAME"), 120.0)
        with self.assertRaises(ValueError):
            bounded_seconds("0", 300.0, 60.0, 3600.0, "NAME")
        with self.assertRaises(ValueError):
            bounded_seconds("100000", 300.0, 60.0, 3600.0, "NAME")
        with self.assertRaises(ValueError):
            bounded_seconds("nan", 300.0, 60.0, 3600.0, "NAME")


class AttachQuotaSourceTest(unittest.TestCase):
    def test_named_organization_attaches_the_source_to_policy(self) -> None:
        server = SimpleNamespace(policy=Policy.load("config/policy.toml"))
        with patch.dict("os.environ", {"CINDRAL_BILLING_ORG": "example-org"}):
            with patch("cindral.quota.BillingClient", FakeBillingClient):
                source = attach_quota_source(server, "token-value")
        try:
            self.assertIs(server.policy.quota_source, source)
            self.assertEqual(source.current().status, QUOTA_AVAILABLE)
        finally:
            source.stop()

    def test_unnamed_organization_attaches_nothing(self) -> None:
        server = SimpleNamespace(policy=Policy.load("config/policy.toml"))
        self.assertIsNone(attach_quota_source(server, "token-value"))
        self.assertIsNone(server.policy.quota_source)


if __name__ == "__main__":
    unittest.main()
