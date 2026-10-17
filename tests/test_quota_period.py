"""Tests for the billing-month reset, which is the one time a reading expires early.

Age alone cannot catch it: an exhausted September reading stays inside the
staleness bound well into October, when the allowance has already come back.
These live apart from the rest of the source's behavior because they share one
setup and the rules are specific to the reset.
"""
import unittest
from datetime import datetime, timezone

from cindral.billing import (
    QUOTA_AVAILABLE,
    BillingClient,
    QUOTA_EXHAUSTED,
    QUOTA_UNKNOWN,
    QuotaFailure,
    QuotaReading,
)
from cindral.quota import POST_RESET_TIMEOUT_SECONDS, QuotaSource

MONTH_END = datetime(2026, 9, 30, 23, 59, 0, tzinfo=timezone.utc).timestamp()
OCTOBER_START = datetime(2026, 10, 1, 0, 4, 0, tzinfo=timezone.utc).timestamp()
SEPTEMBER = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).timestamp()


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
        return outcome


def reading(status: str, observed_at: float, **fields) -> QuotaReading:
    return QuotaReading(status=status, source="actions_billing", observed_at=observed_at, **fields)


class MonthResetTest(unittest.TestCase):
    def source(self, client, clock=None, **kwargs) -> QuotaSource:
        notes: list[str] = []
        source = QuotaSource(
            client, "example-org", clock=clock or Clock(SEPTEMBER), note=notes.append, **kwargs
        )
        source.notes = notes
        return source

    def test_a_reading_from_the_previous_month_is_refused_after_the_reset(self) -> None:
        # the allowance resets on the first of the month, so serving September's
        # exhausted reading into October moved every private push to a device on
        # a schedule an operator could predict
        clock = Clock(MONTH_END)
        exhausted = reading(QUOTA_EXHAUSTED, MONTH_END, period="2026-09", remaining_minutes=0.0)
        source = self.source(ScriptedClient(exhausted), clock=clock, max_age=900.0)
        source.refresh()
        self.assertEqual(source.current().status, QUOTA_EXHAUSTED)
        clock.now = OCTOBER_START  # four minutes into the next month
        current = source.current()
        self.assertEqual(current.status, QUOTA_UNKNOWN)
        self.assertIsNone(current.remaining_minutes)
        self.assertIn("period reset", current.reason)

    def test_the_month_reset_reads_afresh_instead_of_waiting_out_the_interval(self) -> None:
        # refusing the previous month is right, but with nothing triggering a
        # read the quota stayed unknown for up to a refresh interval at the start
        # of every billing month, which is a scheduled window on local routing
        clock = Clock(MONTH_END)
        client = ScriptedClient(
            reading(QUOTA_AVAILABLE, MONTH_END, period="2026-09", remaining_minutes=2695.0),
            reading(QUOTA_AVAILABLE, OCTOBER_START, period="2026-10", remaining_minutes=3000.0),
        )
        source = self.source(client, clock=clock, max_age=86400.0, refresh_interval=3600.0)
        source.refresh()
        clock.now = OCTOBER_START
        current = source.current()
        self.assertEqual(current.status, QUOTA_AVAILABLE)
        self.assertEqual(current.remaining_minutes, 3000.0)
        self.assertEqual(client.reads, 2)

    def test_a_failed_read_after_the_reset_keeps_the_cache_and_says_why(self) -> None:
        # the read runs on the caller's thread, so its failure must leave the
        # cache alone and must still name the month, not report a read failure
        clock = Clock(MONTH_END)
        client = ScriptedClient(
            reading(QUOTA_AVAILABLE, MONTH_END, period="2026-09", remaining_minutes=2695.0),
            "unreachable",
        )
        source = self.source(client, clock=clock, max_age=86400.0, refresh_interval=3600.0)
        source.refresh()
        clock.now = OCTOBER_START
        current = source.current()
        self.assertEqual(current.status, QUOTA_UNKNOWN)
        self.assertIn("period reset", current.reason)
        self.assertIsNone(current.remaining_minutes)
        # the previous reading is still the cache, not discarded by the failure
        self.assertEqual(source._cache.period, "2026-09")
        self.assertEqual(client.reads, 2)

    def test_the_read_after_the_reset_is_bounded_like_the_startup_one(self) -> None:
        # it runs on the route request thread, and a webhook delivery is given
        # seconds rather than the client's own timeout, so the budget is read
        # off the request that actually carries it
        from io import BytesIO
        from unittest.mock import patch
        from urllib.error import HTTPError

        from cindral import billing

        deadlines: list[float] = []
        moved = HTTPError("url", 410, "gone", {}, None)
        usage = b'{"usageItems": [{"product": "actions", "unitType": "minutes", "quantity": 1}]}'

        def record(request, timeout=None):
            deadlines.append(float(timeout))
            body = b'{"id": 1}' if str(request.full_url).endswith("/orgs/example-org") else usage
            if str(request.full_url).endswith("/settings/billing/usage?year=2026&month=9"):
                raise moved
            return BytesIO(body)

        clock = Clock(MONTH_END)
        source = self.source(
            BillingClient("token", clock=clock), clock=clock, max_age=86400.0, refresh_interval=3600.0
        )
        # the enhanced report is the only source that names a month, so it is the
        # only cached reading the reset can invalidate
        source._cache = reading(QUOTA_UNKNOWN, MONTH_END, period="2026-09")
        clock.now = OCTOBER_START
        with patch.object(billing, "urlopen", side_effect=record):
            current = source.current()
        self.assertTrue(deadlines, "the reset must read afresh")
        self.assertEqual(set(deadlines), {POST_RESET_TIMEOUT_SECONDS})
        # the fallback path makes at most three calls, and a webhook delivery is
        # given ten seconds, so the total has to stay inside that
        self.assertLessEqual(POST_RESET_TIMEOUT_SECONDS * 3, 10)
        self.assertEqual(current.period, "2026-10")

    def test_a_legacy_reading_after_the_reset_is_accepted(self) -> None:
        # the legacy endpoint reports no month, and the pre-reset check already
        # accepts that, so the post-reset check must not disagree
        clock = Clock(MONTH_END)
        client = ScriptedClient(
            reading(QUOTA_AVAILABLE, MONTH_END, period="2026-09", remaining_minutes=2695.0),
            reading(QUOTA_AVAILABLE, OCTOBER_START, remaining_minutes=3000.0),
        )
        source = self.source(client, clock=clock, max_age=86400.0, refresh_interval=3600.0)
        source.refresh()
        clock.now = OCTOBER_START
        current = source.current()
        self.assertEqual(current.status, QUOTA_AVAILABLE)
        self.assertEqual(current.remaining_minutes, 3000.0)

    def test_a_legacy_reading_has_no_period_and_is_gated_on_age_alone(self) -> None:
        # the legacy endpoint reports no period, so only age can disqualify it
        clock = Clock(OCTOBER_START)
        legacy = reading(QUOTA_AVAILABLE, MONTH_END, remaining_minutes=2695.0)
        source = self.source(ScriptedClient(legacy), clock=clock, max_age=900.0)
        source.refresh()
        self.assertEqual(source.current().status, QUOTA_AVAILABLE)

    def test_a_reading_carrying_the_current_month_is_served(self) -> None:
        clock = Clock(OCTOBER_START)
        fresh = reading(QUOTA_EXHAUSTED, OCTOBER_START, period="2026-10", remaining_minutes=0.0)
        source = self.source(ScriptedClient(fresh), clock=clock, max_age=900.0)
        source.refresh()
        self.assertEqual(source.current().status, QUOTA_EXHAUSTED)
