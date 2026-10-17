"""The metered ceiling, and the count it is derived from.

The count is dispatches rather than minutes, because a hosted run leaves no
record here of how long it took. That makes the bound pessimistic, which is the
only safe direction for a limit whose crossing costs money, and these tests pin
both the pessimism and the refusal.
"""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import cindral.budget as budget
from cindral.budget import (
    DispatchBudget,
    attach_budget,
    budget_refusal,
    budget_snapshot,
    month_start,
)
from cindral.metering import MeteredLedger
from cindral.models import RouteRequest, Runner
from cindral.policy import Policy, RouteUnavailable

REPO = "example-org/example-app"
# 2026-03-15T12:00:00Z, mid-month and mid-week so a day or timezone bug shows
INSTANT = 1773576000.0
MONTH_START = month_start(INSTANT)


class DispatchCountTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = MeteredLedger(str(Path(self.dir.name) / "jobs.db"))

    def register(self, delivery: str, lane: str, created: float) -> None:
        """Note a dispatch as though it had been made at an instant."""
        self.ledger.reserve(REPO, lane, "cindral-dispatch.yml", "refs/heads/main", now=created)

    def test_only_hosted_dispatches_are_counted(self) -> None:
        self.register("d1", "hosted", INSTANT)
        self.register("d2", "fallback", INSTANT)
        self.register("d3", "device", INSTANT)
        self.register("d4", "burst", INSTANT)
        budget = DispatchBudget(self.ledger, monthly_minutes=100, minutes_per_run=10,
                                clock=lambda: INSTANT)
        self.assertEqual(budget.dispatches(), 1)

    def test_a_dispatch_before_the_month_is_not_counted(self) -> None:
        self.register("d1", "hosted", INSTANT)
        self.register("old", "hosted", MONTH_START - 1)
        budget = DispatchBudget(self.ledger, monthly_minutes=100, minutes_per_run=10,
                                clock=lambda: INSTANT)
        self.assertEqual(budget.dispatches(), 1)

    def test_a_dispatch_on_the_first_instant_of_the_month_is_counted(self) -> None:
        # the boundary is inclusive, so the dispatch a month reset lets through
        # is not silently lost to a strict comparison
        self.register("edge", "hosted", MONTH_START)
        budget = DispatchBudget(self.ledger, monthly_minutes=100, minutes_per_run=10,
                                clock=lambda: INSTANT)
        self.assertEqual(budget.dispatches(), 1)

    def test_a_new_month_starts_over(self) -> None:
        self.register("d1", "hosted", INSTANT)
        self.register("d2", "hosted", INSTANT)
        budget = DispatchBudget(self.ledger, monthly_minutes=15, minutes_per_run=10,
                                clock=lambda: INSTANT)
        self.assertTrue(budget.reached())
        next_month = DispatchBudget(self.ledger, monthly_minutes=15, minutes_per_run=10,
                                   clock=lambda: MONTH_START + 86400 * 40)
        self.assertEqual(next_month.dispatches(), 0)
        self.assertFalse(next_month.reached())

    def test_the_assumed_duration_sets_the_bound(self) -> None:
        for index in range(3):
            self.register(f"d{index}", "hosted", INSTANT)
        cheap = DispatchBudget(self.ledger, monthly_minutes=100, minutes_per_run=10,
                               clock=lambda: INSTANT)
        dear = DispatchBudget(self.ledger, monthly_minutes=100, minutes_per_run=50,
                              clock=lambda: INSTANT)
        self.assertFalse(cheap.reached())
        self.assertTrue(dear.reached())
        self.assertEqual(cheap.spent_minutes(), 30)
        self.assertEqual(dear.spent_minutes(), 150)

    def test_a_dispatch_that_reaches_the_ceiling_exactly_is_refused(self) -> None:
        # "once the month's ceiling is reached", so reaching it is the crossing.
        # Spending the allowance exactly would be the one dispatch whose cost is
        # unknown until it is over, and this is a limit whose crossing costs
        # money, so the bound errs towards stopping.
        for index in range(3):
            self.register(f"d{index}", "hosted", INSTANT)
        budget = DispatchBudget(self.ledger, monthly_minutes=30, minutes_per_run=10,
                                clock=lambda: INSTANT)
        self.assertEqual(budget.spent_minutes(), 30)
        self.assertTrue(budget.reached(), "the fourth dispatch would reach the ceiling")
        self.assertEqual(budget.remaining_minutes(), 0.0)

    def test_the_estimate_is_counted_against_the_budget(self) -> None:
        # a budget with room for exactly one short run must not admit a long one
        self.register("d1", "hosted", INSTANT)
        budget = DispatchBudget(self.ledger, monthly_minutes=25, minutes_per_run=10,
                                clock=lambda: INSTANT)
        self.assertFalse(budget.reached(estimated_minutes=10))
        self.assertTrue(budget.reached(estimated_minutes=20))


class WiringTest(unittest.TestCase):
    """The path from a policy file to an enforced ceiling.

    Every other test builds the policy directly, which leaves this one
    uncovered: if the wiring never ran, or ran before the store existed, the
    feature would be inert and every other test would still pass.
    """

    def policy_file(self, quota_line: str | None):
        """A policy on disk, because the ceiling is a file setting not a default."""
        from cindral.policy import Policy
        if not hasattr(self, "dir"):
            self.dir = tempfile.TemporaryDirectory()
            self.addCleanup(self.dir.cleanup)
        body = (
            '[hosted]\nrunner = "ubuntu-24.04"\n\n'
            '[local]\npriority = ["fallback"]\n\n'
            '[lanes.fallback]\nlabels = ["self-hosted", "Linux", "ARM64", "fallback"]\n\n'
            "[quota]\n"
        )
        if quota_line:
            body += quota_line + "\n"
        path = Path(self.dir.name) / "policy.toml"
        path.write_text(body)
        return Policy.load(path)

    def server(self, budget: str | None):
        from cindral.service import CindralHandler, CindralServer
        server = CindralServer(("127.0.0.1", 0), CindralHandler)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        server.metered = MeteredLedger(str(Path(self.dir.name) / "jobs.db"))
        server.policy = self.policy_file(budget)
        return server

    def test_a_configured_rate_changes_the_dispatch_count_not_just_a_field(self) -> None:
        """The rate is the one figure an operator can get wrong, so it must count.

        The ceiling holds only if the assumption is right. Real runs costing more
        than assumed spend past the ceiling before the count says so, and runs
        costing less are refused while allowance remains. A rate that was read into
        a field and then ignored by the arithmetic would leave both failures
        invisible, so this asserts the dispatch count the rate produces.
        """
        server = self.server("monthly_minute_budget = 100.0\nminutes_per_run = 25.0")
        budget = attach_budget(server, server.metered)
        self.assertIsNotNone(budget)
        self.assertEqual(server.policy.minutes_per_run, 25.0)
        self.assertEqual(budget.minutes_per_run, 25.0)
        # 100 minutes at 25 per dispatch is four dispatches, not ten
        for index in range(4):
            server.metered.reserve(REPO, "hosted", "cindral-dispatch.yml", "refs/heads/main")
        self.assertEqual(budget.dispatches(), 4)
        self.assertEqual(budget.spent_minutes(), 100.0)
        self.assertTrue(budget.reached())

    def test_the_configured_rate_is_the_rate_the_refusal_uses(self) -> None:
        """Enforcement reads the policy, not the budget object, and both must agree.

        The check `budget_refusal` performs is what actually refuses a dispatch, and
        it reads `policy.assumed_minutes_per_run` rather than the budget's own field.
        A rate that reached only the budget object would leave the arithmetic
        configurable and the enforcement unchanged, which is a limit that reads as
        configured and behaves as a constant. So this drives the refusal, not the
        object: at 25 minutes per dispatch a 100 minute budget refuses the third
        dispatch, where the default rate of 10 would still be allowing a fourth.
        """
        server = self.server("monthly_minute_budget = 100.0\nminutes_per_run = 25.0")
        attach_budget(server, server.metered)
        request = RouteRequest(requested_lane="hosted")
        runner = Runner(name="desktop", status="online", busy=False, labels=("self-hosted",))

        allowed = []
        for index in range(4):
            server.metered.reserve(REPO, "hosted", "cindral-dispatch.yml", "refs/heads/main")
            allowed.append(budget_refusal(server.policy) is None)

        # Refused on the third, not the fourth: a caller's estimate is raised to the
        # rate the ceiling already assumes, so at 75 minutes used a fourth dispatch
        # would cross 100 and is refused rather than admitted and overspent.
        self.assertEqual(allowed, [True, True, False, False])
        self.assertEqual(server.policy.assumed_minutes_per_run, 25.0)
        # the refusal names the rate, because a person being turned away is exactly
        # who needs to know the minutes used is a count converted at an assumption
        self.assertIn("25 minutes per dispatch", budget_refusal(server.policy) or "")

    def test_the_refusal_rate_tracks_the_configured_rate_not_the_default(self) -> None:
        """The same month and the same dispatches, two rates, two different answers.

        This is the discriminator for the previous test: nothing else differs, so a
        rate that only reached the budget object would leave both sides allowing the
        fourth dispatch and the default would look configured.
        """
        for rate, fourth in (("", True), ("minutes_per_run = 25.0\n", False)):
            with self.subTest(rate=rate or "default"):
                server = self.server(f"monthly_minute_budget = 100.0\n{rate}")
                attach_budget(server, server.metered)
                for _ in range(4):
                    server.metered.reserve(
                        REPO, "hosted", "cindral-dispatch.yml", "refs/heads/main"
                    )
                self.assertEqual(budget_refusal(server.policy) is None, fourth)

    def test_an_unset_rate_falls_back_to_the_documented_default(self) -> None:
        from cindral.policy import DEFAULT_MINUTES_PER_RUN
        server = self.server("monthly_minute_budget = 100.0")
        attach_budget(server, server.metered)
        self.assertEqual(server.policy.minutes_per_run, DEFAULT_MINUTES_PER_RUN)
        self.assertEqual(DEFAULT_MINUTES_PER_RUN, 10.0)

    def test_the_month_is_readable_before_anything_is_refused(self) -> None:
        """A ceiling visible only in its refusal is known only after it costs.

        The refusal names the month and the minutes used, but a refusal happens
        once a dispatch has been turned away. Someone asking whether the month is
        nearly spent has no way to ask, so the figures are reported whether or not
        anything is currently being refused.
        """
        server = self.server("monthly_minute_budget = 100.0\nminutes_per_run = 10.0")
        attach_budget(server, server.metered)
        for _ in range(3):
            server.metered.reserve(REPO, "hosted", "cindral-dispatch.yml", "refs/heads/main")

        snapshot = budget_snapshot(server.policy)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["used_minutes"], 30.0)
        self.assertEqual(snapshot["remaining_minutes"], 70.0)
        self.assertEqual(snapshot["budget_minutes"], 100.0)
        self.assertEqual(snapshot["assumed_minutes_per_dispatch"], 10.0)
        self.assertIn("month", snapshot)
        # nothing has been refused at 30 of 100 minutes, and the month is still legible
        self.assertIsNone(budget_refusal(server.policy))

    def test_remaining_minutes_are_not_read_as_permission(self) -> None:
        """A remainder is arithmetic; whether one more dispatch fits is permission.

        At 25 minutes per dispatch a month with 25 remaining admits nothing, because
        admitting one would cross the ceiling. Reporting the remainder alone tells
        an operator there is room when there is none, which is the reading this
        figure was added to replace, and it reads the same on /healthz as a genuine
        25 minutes of headroom under a lower rate.
        """
        server = self.server("monthly_minute_budget = 100.0\nminutes_per_run = 25.0")
        attach_budget(server, server.metered)
        for _ in range(3):
            server.metered.reserve(REPO, "hosted", "cindral-dispatch.yml", "refs/heads/main")

        snapshot = budget_snapshot(server.policy)
        self.assertEqual(snapshot["remaining_minutes"], 25.0)
        self.assertFalse(snapshot["next_dispatch_admitted"])
        # and the refusal agrees, so the reported figure cannot contradict the check
        self.assertIsNotNone(budget_refusal(server.policy))

    def test_a_remainder_with_room_reports_that_a_dispatch_is_admitted(self) -> None:
        server = self.server("monthly_minute_budget = 100.0")
        attach_budget(server, server.metered)
        for _ in range(7):
            server.metered.reserve(REPO, "hosted", "cindral-dispatch.yml", "refs/heads/main")

        snapshot = budget_snapshot(server.policy)
        self.assertEqual(snapshot["remaining_minutes"], 30.0)
        self.assertTrue(snapshot["next_dispatch_admitted"])
        self.assertIsNone(budget_refusal(server.policy))

    def test_the_reported_admission_is_the_ceiling_own_answer_not_a_recomputation(self) -> None:
        """The figure is asked of the refusal, so the two cannot drift apart.

        A snapshot that recomputes the comparison is a second implementation of the
        rule, and two implementations of a spending ceiling eventually disagree in
        the direction that spends. Every count in the month's life agrees, and the
        reported answer is the one the check gives.
        """
        server = self.server("monthly_minute_budget = 100.0\nminutes_per_run = 25.0")
        attach_budget(server, server.metered)
        for index in range(5):
            server.metered.reserve(REPO, "hosted", "cindral-dispatch.yml", "refs/heads/main")
            snapshot = budget_snapshot(server.policy)
            self.assertEqual(
                snapshot["next_dispatch_admitted"],
                budget_refusal(server.policy) is None,
                f"the snapshot and the refusal disagreed after {index + 1} dispatches",
            )

    def test_the_snapshot_reads_the_ledger_once(self) -> None:
        """Two reads can describe two different months.

        The snapshot reports the count and whether another dispatch fits. Reading the
        count and then asking the refusal reads the ledger twice, and a dispatch
        committed in between leaves the reported minutes and the reported answer
        describing different moments: a snapshot saying 70 minutes used and another
        dispatch admitted, taken either side of a dispatch that has since been
        counted. The count and the answer have to come from one read.
        """
        server = self.server("monthly_minute_budget = 100.0")
        attach_budget(server, server.metered)
        reads = 0
        counted = server.policy.metered_minutes

        def counting():
            nonlocal reads
            reads += 1
            return counted()

        server.policy = replace(server.policy, metered_minutes=counting)

        budget_snapshot(server.policy)
        self.assertEqual(reads, 1, f"the snapshot read the count {reads} times")

        # the check still reads exactly once, so neither path is paying for the other
        budget_refusal(server.policy)
        self.assertEqual(reads, 2, "the check should add one read of its own")

    def test_the_snapshot_and_the_check_agree_with_one_read_each(self) -> None:
        """The shared rule, checked where the two paths meet.

        The snapshot reads once and the check reads once, so agreement is no longer
        free: it holds because both call the same comparison on a count they each
        read, and it would stop holding if either grew a second implementation.
        """
        server = self.server("monthly_minute_budget = 100.0\nminutes_per_run = 25.0")
        attach_budget(server, server.metered)
        for index in range(5):
            server.metered.reserve(REPO, "hosted", "cindral-dispatch.yml", "refs/heads/main")
            self.assertEqual(
                budget_snapshot(server.policy)["next_dispatch_admitted"],
                budget_refusal(server.policy) is None,
                f"the snapshot and the refusal disagreed after {index + 1} dispatches",
            )

    def test_a_count_that_cannot_be_read_degrades_instead_of_failing_the_probe(self) -> None:
        """A health probe has to answer, and the count is a database read.

        Reporting the month put a SQLite read in front of /healthz, so a locked or
        closed database turned into a failed health check and the pod left rotation
        for a figure nobody can read. The figure is omitted and the reason named, so
        "I cannot tell" is visibly different from "the month is empty" rather than
        being a 500 that reads as unhealthy.
        """
        import sqlite3

        def locked():
            raise sqlite3.OperationalError("database is locked")

        server = self.server("monthly_minute_budget = 100.0")
        attach_budget(server, server.metered)
        server.policy = replace(server.policy, metered_minutes=locked)

        snapshot = budget_snapshot(server.policy)
        self.assertIsNotNone(snapshot)
        self.assertIn("unavailable", snapshot)
        self.assertIn("database is locked", snapshot["unavailable"])
        # the figures are absent rather than zero: zero would read as a month spent
        self.assertNotIn("used_minutes", snapshot)
        self.assertNotIn("remaining_minutes", snapshot)

    def test_a_count_that_cannot_be_read_refuses_rather_than_allows(self) -> None:
        """Reporting degrades; enforcement must do the opposite.

        A ceiling that cannot read its own count must not treat that as room left,
        or a database that briefly fails becomes unlimited spend. The two paths
        disagree on purpose: the probe loses a figure, the check loses a dispatch.
        """
        import sqlite3

        def locked():
            raise sqlite3.OperationalError("database is locked")

        server = self.server("monthly_minute_budget = 100.0")
        attach_budget(server, server.metered)
        server.policy = replace(server.policy, metered_minutes=locked)

        with self.assertRaises(sqlite3.OperationalError):
            budget_refusal(server.policy)

    def test_no_ceiling_means_no_month_to_report(self) -> None:
        server = self.server(None)
        self.assertIsNone(attach_budget(server, server.metered))
        self.assertIsNone(budget_snapshot(server.policy))

    def test_a_rate_that_is_not_a_positive_number_is_refused(self) -> None:
        """A rate of zero would admit every dispatch while reading as a limit."""
        for value in ("0", "-5", '"ten"', "inf", "nan"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.policy_file(f"monthly_minute_budget = 100.0\nminutes_per_run = {value}")

    def test_a_configured_ceiling_reaches_the_policy(self) -> None:
        server = self.server("monthly_minute_budget = 25.0")
        attached = attach_budget(server, server.metered)
        self.assertIsNotNone(attached)
        self.assertIsNotNone(server.policy.metered_minutes)

    def test_the_wired_count_is_live_rather_than_read_once(self) -> None:
        # a closure, so a dispatch recorded after startup counts, and a month
        # reset lifts the ceiling without a reload
        server = self.server("monthly_minute_budget = 25.0")
        attach_budget(server, server.metered)
        request = RouteRequest(requested_lane="hosted")
        runner = Runner(name="desktop", status="online", busy=False,
                        labels=("self-hosted", "Linux", "ARM64", "fallback"), healthy=True)
        self.assertEqual(server.policy.choose(request, (runner,)).lane, "hosted")
        for _ in range(3):
            server.metered.reserve(REPO, "hosted", "cindral-dispatch.yml", "refs/heads/main")
        decision = server.policy.choose(request, (runner,))
        self.assertNotEqual(decision.lane, "hosted")
        self.assertIn("25", decision.reason)

    def test_a_wired_ceiling_publishes_the_rate_it_assumes(self) -> None:
        # the floor has to come from the wiring and not from a default, or a
        # broker configured with a higher rate would still admit a small
        # estimate
        server = self.server("monthly_minute_budget = 25.0")
        attach_budget(server, server.metered)
        self.assertEqual(server.policy.assumed_minutes_per_run, 10.0)
        spent = replace(server.policy, metered_minutes=lambda: 10)
        self.assertIsNone(budget_refusal(spent, 0))
        self.assertIsNone(budget_refusal(spent, 5))
        nearly = replace(server.policy, metered_minutes=lambda: 16)
        self.assertIsNotNone(budget_refusal(nearly, 0), "a zero estimate was free")

    def test_a_budget_that_is_not_a_number_is_refused_at_load(self) -> None:
        # every comparison against nan or infinity is false, so a ceiling holding
        # one admits every metered dispatch while reading as a limit. That is the
        # worst shape a limit can have, so it is refused where the operator can
        # see it rather than counted against
        # TOML itself refuses some of these before the policy sees them, which is
        # the right place for it, so both refusals count as the ceiling holding
        for value in ("nan", "NaN", "inf", "-inf", "-5"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.policy_file(f"monthly_minute_budget = {value}")

    def test_a_budget_that_is_not_a_number_at_all_is_refused_at_load(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be a number of minutes"):
            self.policy_file('monthly_minute_budget = "lots"')

    def test_a_zero_budget_is_allowed_and_refuses_everything(self) -> None:
        # a policy that means it can say so, which is why an absent budget is
        # None rather than zero
        server = self.server("monthly_minute_budget = 0")
        self.assertEqual(server.policy.minute_budget, 0.0)
        attach_budget(server, server.metered)
        self.assertIsNotNone(
            budget_refusal(replace(server.policy, metered_minutes=lambda: 0.0), 0)
        )

    def test_no_ceiling_configured_leaves_the_policy_untouched(self) -> None:
        server = self.server(None)
        self.assertIsNone(attach_budget(server, server.metered))
        self.assertIsNone(server.policy.metered_minutes)

    def test_a_ceiling_with_nowhere_to_count_is_refused_rather_than_declined(self) -> None:
        # a ceiling nobody can enforce is worse than no ceiling, because it reads
        # as one. Wiring it without a ledger raises at startup, where the
        # operator finds out, rather than at the first metered dispatch
        from cindral.service import CindralHandler, CindralServer
        server = CindralServer(("127.0.0.1", 0), CindralHandler)
        server.metered = None
        server.policy = self.policy_file("monthly_minute_budget = 25.0")
        with self.assertRaisesRegex(ValueError, "no ledger"):
            attach_budget(server, None)


class BudgetRefusalTest(unittest.TestCase):
    def policy(self, budget: float | None, used: float) -> Policy:
        return replace(Policy.load("config/policy.toml"),
                       minute_budget=budget, metered_minutes=lambda: used)

    def test_no_configured_ceiling_never_refuses(self) -> None:
        self.assertIsNone(budget_refusal(self.policy(None, 10_000), 10))

    def test_a_count_below_the_ceiling_is_allowed(self) -> None:
        self.assertIsNone(budget_refusal(self.policy(100, 50), 10))

    def test_a_count_at_the_ceiling_is_refused_and_names_the_month(self) -> None:
        refusal = budget_refusal(self.policy(100, 100), 10)
        self.assertIsNotNone(refusal)
        self.assertIn("100", refusal)
        self.assertIn("this month", refusal)

    def test_an_explicit_hosted_request_is_refused_too(self) -> None:
        # this is the whole point: a caller naming the hosted lane must not
        # bypass a ceiling, and the push path names it explicitly
        policy = self.policy(100, 100)
        runner = Runner(name="desktop", status="online", busy=False,
                        labels=("self-hosted", "Linux", "ARM64", "fallback"), healthy=True)
        decision = policy.choose(RouteRequest(requested_lane="hosted"), (runner,))
        self.assertNotEqual(decision.lane, "hosted")
        self.assertIn("budget", decision.reason)

    def test_an_estimate_below_the_assumed_dispatch_is_raised_to_it(self) -> None:
        # the bound is a count of dispatches converted at a fixed duration, so a
        # caller that supplies no estimate, or a small one, must not buy a
        # dispatch the arithmetic says does not fit. 30 used with 35 allowed
        # fits only if the next dispatch is free, and it is not.
        policy = replace(self.policy(35, 30), assumed_minutes_per_run=10)
        self.assertIsNotNone(budget_refusal(policy, 0), "a zero estimate was free")
        self.assertIsNotNone(budget_refusal(policy, 4), "a four minute estimate was free")
        self.assertIsNotNone(budget_refusal(policy, 10))
        # and with room, an estimate below the assumption still goes through,
        # because the floor is a floor and not a surcharge
        roomy = replace(self.policy(35, 20), assumed_minutes_per_run=10)
        self.assertIsNone(budget_refusal(roomy, 0))
        self.assertIsNone(budget_refusal(roomy, 14))
        self.assertIsNotNone(budget_refusal(roomy, 15))

    def test_a_spent_ceiling_refuses_where_there_is_no_local_runner(self) -> None:
        # with nowhere to fall back to, the answer has to be no. Spending is not
        # an option the ceiling leaves open.
        policy = self.policy(100, 100)
        with self.assertRaises(RouteUnavailable):
            policy.choose(RouteRequest(requested_lane="hosted"), ())

    def test_a_ceiling_is_refused_rather_than_raising(self) -> None:
        with self.assertRaises(ValueError):
            DispatchBudget(MeteredLedger(":memory:"), monthly_minutes=-1)
        with self.assertRaises(ValueError):
            DispatchBudget(MeteredLedger(":memory:"), monthly_minutes=10, minutes_per_run=0)
