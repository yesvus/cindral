"""The ceiling has to count every path that spends, not just the one that logged.

The push webhook was the only path that left a record for the count to read, so
`/v1/dispatch` and a pull request both dispatched to a metered lane and moved the
count by nothing. The check passed forever and two thirds of the spend was
unbounded. These drive the real handler over all three so the wiring is what is
tested rather than the function underneath it.
"""
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from cindral.github import GitHubDispatchError
from cindral.budget import DispatchBudget, attach_budget, month_start
from cindral.policy import Policy, RouteUnavailable
from cindral.deliveries import DeliveryStore
from cindral.metering import MeteredLedger
from cindral.service import WEBHOOK_PATH, CindralHandler, CindralServer
from cindral.webhook_dispatch import dispatch_record, recover_ready
from cindral.state import load_runners
from cindral.webhook import sign

REPO = "example-org/example-app"
WORKFLOW = "cindral-dispatch.yml"
SECRET = "secret"
INSTANT = 1773576000.0

#: hosted quota unknown and the policy answering "hosted" for it, so the metered
#: lane is reachable in a test. The shipped policy says local, which is the right
#: default for an account that cannot read its own allowance.
POLICY = """
[hosted]
runner = "ubuntu-24.04"

[quota]
unknown_quota = "hosted"
monthly_minute_budget = 40.0

[local]
priority = ["fallback"]

[lanes.fallback]
labels = ["self-hosted", "Linux", "ARM64", "fallback"]

[[dispatch.repositories]]
mode = "both"
repository = "example-org/example-app"
workflows = ["cindral-dispatch.yml"]
refs = ["refs/heads/main"]
"""


class MeteredPathsTest(unittest.TestCase):
    """Three ways to spend, and one count that has to see all of them."""

    def setUp(self) -> None:
        self.db = tempfile.TemporaryDirectory()
        self.addCleanup(self.db.cleanup)
        path = str(Path(self.db.name) / "jobs.db")
        self.server = CindralServer(("127.0.0.1", 0), CindralHandler)
        self.server.deliveries = DeliveryStore(path)
        self.server.metered = MeteredLedger(path)
        self.policy_path = Path(self.db.name) / "policy.toml"
        self.policy_path.write_text(POLICY, encoding="utf-8")
        self.server.policy = Policy.load(self.policy_path)
        self.budget = attach_budget(self.server, self.server.metered)
        self.server.runners = load_runners("examples/state.json")
        self.server.github = None
        self.server.webhook_secret = SECRET
        self.server.dispatch_token = "dispatch-token"
        self.server.workflow_file = WORKFLOW
        self.server.capacity_lock = threading.Lock()
        self.server.runner_reservations = {}
        self.server.reservation_seconds = 10
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def post(self, path: str, body: bytes, headers: dict | None = None):
        import urllib.request

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method="POST"
        )
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read() or b"{}")

    def signed(self, body: bytes, event: str) -> dict:
        return {
            "X-Hub-Signature-256": sign(SECRET, body),
            "X-GitHub-Event": event,
            "Content-Type": "application/json",
        }

    def push_body(self) -> bytes:
        return json.dumps(
            {
                "ref": "refs/heads/main",
                "after": "0" * 40,
                "deleted": False,
                "repository": {
                    "full_name": REPO,
                    "private": True,
                    "default_branch": "main",
                },
            }
        ).encode()

    def pull_body(self) -> bytes:
        return json.dumps(
            {
                "action": "opened",
                "number": 42,
                "repository": {
                    "full_name": REPO,
                    "private": True,
                    "default_branch": "main",
                },
                "pull_request": {
                    "author_association": "OWNER",
                    "draft": False,
                    "head": {"sha": "a" * 40, "repo": {"full_name": REPO}},
                    "base": {"sha": "b" * 40, "ref": "refs/heads/main"},
                },
            }
        ).encode()

    def dispatch_body(self, lane: str = "hosted") -> bytes:
        return json.dumps(
            {
                "repository": REPO,
                "workflow": WORKFLOW,
                "ref": "main",
                "requested_lane": lane,
                # the caller's own reading of its allowance, which is the one
                # number the broker cannot work out for itself
                "quota_status": "available",
            }
        ).encode()

    def spend_one(self, send) -> None:
        """One metered dispatch through `send`, with GitHub accepting it."""
        with patch.object(self.server, "github") as github:
            github.dispatch.return_value = {}
            github.list_runners.return_value = ()
            status, _payload = send()
        self.assertEqual(status, 200, f"the dispatch was not accepted ({_payload}), so nothing was spent")

    def spent(self) -> int:
        return DispatchBudget(
            self.server.metered, monthly_minutes=1e9, clock=lambda: INSTANT
        ).dispatches()

    def test_the_explicit_dispatch_endpoint_counts(self) -> None:
        # this is one of the two paths that moved nothing. A ceiling that cannot
        # see it is not a ceiling
        self.assertEqual(self.spent(), 0)
        self.spend_one(
            lambda: self.post(
                "/v1/dispatch",
                self.dispatch_body(),
                {"Authorization": "Bearer dispatch-token"},
            )
        )
        self.assertEqual(self.spent(), 1, "an explicit metered dispatch was not counted")

    def test_a_pull_request_counts(self) -> None:
        self.assertEqual(self.spent(), 0)
        self.spend_one(
            lambda: self.post(WEBHOOK_PATH, self.pull_body(), self.signed(self.pull_body(), "pull_request"))
        )
        self.assertEqual(self.spent(), 1, "a pull request dispatch was not counted")

    def test_a_push_counts(self) -> None:
        self.assertEqual(self.spent(), 0)
        self.spend_one(
            lambda: self.post(WEBHOOK_PATH, self.push_body(), self.signed(self.push_body(), "push"))
        )
        self.assertEqual(self.spent(), 1, "a push dispatch was not counted")

    def test_every_path_together_crosses_the_ceiling(self) -> None:
        # the point of the three separate tests above is that each one fails on
        # its own. This is the property they exist for: the fourth spend is the
        # one that gets refused.
        for send in (
            lambda: self.post(WEBHOOK_PATH, self.push_body(), self.signed(self.push_body(), "push")),
            lambda: self.post(WEBHOOK_PATH, self.pull_body(), self.signed(self.pull_body(), "pull_request")),
            lambda: self.post(
                "/v1/dispatch", self.dispatch_body(), {"Authorization": "Bearer dispatch-token"}
            ),
        ):
            with patch.object(self.server, "github") as github:
                github.dispatch.return_value = {}
                github.list_runners.return_value = ()
                status, _ = send()
            self.assertEqual(status, 200)
        self.assertEqual(self.spent(), 3)
        # 40 minutes of budget and 10 assumed per dispatch, so three go through
        # and the fourth is the one that crosses
        with patch.object(self.server, "github") as github:
            github.dispatch.return_value = {}
            github.list_runners.return_value = ()
            status, payload = self.post(
                "/v1/dispatch", self.dispatch_body(), {"Authorization": "Bearer dispatch-token"}
            )
        self.assertNotEqual(status, 200, "the ceiling did not stop the fourth metered dispatch")
        # the cause has to survive to the caller, or an operator looking at a
        # refused dispatch cannot tell a spent ceiling from an absent allowance
        self.assertIn("budget", json.dumps(payload))
        self.assertEqual(self.spent(), 3, "a refused dispatch was counted as spend")

    def test_a_replayed_push_records_the_dispatch_it_makes(self) -> None:
        # A redelivery that goes out this time is a second run and a second
        # charge, and it takes a different branch from the first attempt: the
        # record already exists, so the replay is the only path that claims it.
        # Without a count here the ceiling under-reads by exactly the dispatches
        # that took two attempts, which is what a broker restart in the middle of
        # one looks like.
        body = self.push_body()
        headers = dict(self.signed(body, "push"), **{"X-GitHub-Delivery": "delivery-1"})
        # the fingerprint is the body hash, because a record whose fingerprint
        # disagrees is refused as a different payload rather than replayed
        self.server.deliveries.register(
            REPO, "delivery-1", hashlib.sha256(body).hexdigest(), WORKFLOW,
            "refs/heads/main",
            {"cindral_lane": "hosted", "cindral_target": "", "cindral_reason": "replay"},
        )
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = True
            github.dispatch.return_value = {}
            github.list_runners.return_value = ()
            status, _ = self.post(WEBHOOK_PATH, body, headers)
        # 202 because a record that already existed was replayed rather than
        # newly registered, which is the branch under test
        self.assertEqual(status, 202)
        self.assertEqual(self.spent(), 1, "the replayed push was not counted")

    def test_a_redelivery_of_an_accepted_delivery_holds_nothing(self) -> None:
        # the hold is taken before the claim, so a redelivery that dispatches
        # nothing has to give it back or the month is charged for a run that was
        # never going to happen
        body = self.push_body()
        headers = dict(self.signed(body, "push"), **{"X-GitHub-Delivery": "delivery-2"})
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = True
            github.dispatch.return_value = {}
            github.list_runners.return_value = ()
            first, _ = self.post(WEBHOOK_PATH, body, headers)
        self.assertEqual(first, 200)
        self.assertEqual(self.spent(), 1)
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = True
            github.dispatch.return_value = {}
            github.list_runners.return_value = ()
            second, _ = self.post(WEBHOOK_PATH, body, headers)
        self.assertEqual(second, 202)
        self.assertEqual(self.spent(), 1, "a redelivery of an accepted delivery was charged")

    def test_an_unexpected_failure_on_a_pull_request_keeps_the_hold(self) -> None:
        # A rejection from GitHub is a definite answer that the run did not
        # start. A connection that failed part way through is not, and it may have
        # started one, so the hold stays: a ceiling that is one dispatch tight
        # costs a dispatch, and a ceiling that is one loose books a run nobody paid
        # for.
        body = self.pull_body()
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = True
            github.dispatch.side_effect = TimeoutError("connection reset")
            github.post_status.return_value = {}
            github.list_runners.return_value = ()
            status, _ = self.post(WEBHOOK_PATH, body, self.signed(body, "pull_request"))
        self.assertNotEqual(status, 200)
        self.assertEqual(self.spent(), 1, "a dispatch of unknown outcome gave its hold back")

    def test_a_redelivery_that_cannot_be_read_releases_its_hold(self) -> None:
        # the twin of the recovery case, on the path that goes through the
        # endpoint. A hold stranded here is taken again on every replay pass,
        # because the delivery stays ready
        body = self.push_body()
        headers = dict(self.signed(body, "push"), **{"X-GitHub-Delivery": "delivery-rd"})
        with patch.object(self.server.deliveries, "claim", side_effect=sqlite3.Error("locked")):
            with patch.object(self.server, "github") as github:
                github.workflow_exists.return_value = True
                github.list_runners.return_value = ()
                status, _ = self.post(WEBHOOK_PATH, body, headers)
        self.assertNotEqual(status, 200, "the redelivery was answered rather than refused")
        self.assertEqual(self.spent(), 0, "a redelivery that dispatched nothing kept its hold")

    def test_a_dispatch_github_refused_is_not_counted(self) -> None:
        # nothing ran, so nothing was charged, and counting it would make the
        # ceiling tighter than the money that was actually spent
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = True
            github.dispatch.side_effect = GitHubDispatchError("rejected", status_code=422)
            github.list_runners.return_value = ()
            status, _ = self.post(WEBHOOK_PATH, self.push_body(), self.signed(self.push_body(), "push"))
        self.assertNotEqual(status, 200)
        self.assertEqual(self.spent(), 0, "a refused dispatch was counted as spend")

    def test_two_dispatches_at_the_same_moment_cannot_both_pass_the_ceiling(self) -> None:
        # a check that reads the month and a record written afterwards are two
        # steps, and two requests arriving together both read the same remaining
        # budget. The hold is taken under the lock that guards the read, so this
        # is the shape the ceiling has to survive.
        from cindral.budget import reserve_metered
        from dataclasses import replace as dataclass_replace

        # room for one dispatch at the assumed 10 minutes, and no more, so the
        # second one has to be turned away rather than merely counted
        self.server.policy = dataclass_replace(self.server.policy, minute_budget=15.0)
        results: list = []
        ready = threading.Barrier(2)

        def take() -> None:
            ready.wait(10)
            results.append(reserve_metered(self.server.policy, 10))

        workers = [threading.Thread(target=take) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(15)
        self.assertEqual(len(results), 2, "a dispatch did not reach the ceiling check")
        allowed = [hold for hold, refusal in results if refusal is None]
        refused = [refusal for hold, refusal in results if refusal is not None]
        self.assertEqual(len(allowed), 1, f"both dispatches went through: {results}")
        self.assertEqual(len(refused), 1, f"neither dispatch was refused: {results}")
        self.assertIsNotNone(refused[0])
        # and the month holds exactly one dispatch, not two
        self.assertEqual(self.spent(), 1)
        self.assertIsNotNone(
            reserve_metered(self.server.policy, 10)[1],
            "the ceiling did not stop a third dispatch after the budget",
        )

    def test_a_request_that_cannot_be_routed_leaves_no_hold_behind(self) -> None:
        # a refused request has spent nothing, and a hold left behind would
        # tighten the month for every request that came after it
        from cindral.models import RouteRequest
        with self.assertRaises(RouteUnavailable):
            self.server.policy.choose(RouteRequest(requested_lane="device"), ())
        self.assertEqual(self.spent(), 0, "a request that never dispatched was held")

    def test_a_hosted_dispatch_with_room_in_the_budget_is_admitted(self) -> None:
        # the hold is taken before the lane is known, so the case to pin is that
        # a ceiling with room in it admits a hosted dispatch and keeps its hold
        with patch.object(self.server, "github") as github:
            github.dispatch.return_value = {}
            github.list_runners.return_value = ()
            status, payload = self.post(
                "/v1/dispatch", self.dispatch_body(), {"Authorization": "Bearer dispatch-token"}
            )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["lane"], "hosted")
        self.assertEqual(self.spent(), 1)

    def test_a_request_that_routes_to_a_free_lane_holds_nothing(self) -> None:
        # The hold is taken before the lane is known, so the case to pin is a
        # dispatch that ends up on a lane nobody pays for: it has to give the
        # hold back, or the month is charged for a device run.
        from cindral.models import Runner
        device = Runner("device-beta", "online", False,
                        ("self-hosted", "Linux", "ARM64"), True)
        self.server.runners = (device,)
        with patch.object(self.server, "github") as github:
            github.dispatch.return_value = {}
            github.list_runners.return_value = ()
            status, _ = self.post(
                "/v1/dispatch",
                json.dumps({
                    "repository": REPO, "workflow": WORKFLOW, "ref": "main",
                    "requested_lane": "device", "target": "device-beta",
                }).encode(),
                {"Authorization": "Bearer dispatch-token"},
            )
        self.assertIn(status, (200, 409))
        self.assertEqual(self.spent(), 0, "a dispatch on a free lane was charged to the month")

    def test_only_a_definite_refusal_gives_the_hold_back(self) -> None:
        # a 5xx and a connection that failed part way through may both have
        # started a run, and a run that exists has to be counted
        for error, expected in (
            (GitHubDispatchError("rejected", status_code=422), 0),
            (GitHubDispatchError("bad request", status_code=400), 0),
            (GitHubDispatchError("connection refused"), 0),
            (GitHubDispatchError("gateway", status_code=502), 1),
            (TimeoutError("connection reset"), 1),
        ):
            with self.subTest(error=error):
                # each case starts from an empty month, so the count is this
                # dispatch's alone
                with self.server.metered._connect() as connection:
                    connection.execute("DELETE FROM metered_dispatches")
                self.server.policy = replace(self.server.policy, metered_minutes=lambda: 0.0)
                with patch.object(self.server, "github") as github:
                    github.dispatch.side_effect = error
                    github.list_runners.return_value = ()
                    status, _ = self.post(
                        "/v1/dispatch", self.dispatch_body(),
                        {"Authorization": "Bearer dispatch-token"},
                    )
                self.assertNotEqual(status, 200)
                self.assertEqual(self.spent(), expected, f"{error} released or kept wrongly")

    def test_a_device_dispatch_is_not_counted_against_the_metered_ceiling(self) -> None:
        # the ceiling is on the lane that costs money. Counting the device lane
        # would stop a device run for someone else's hosted minutes
        self.assertEqual(self.spent(), 0)
        with patch.object(self.server, "github") as github:
            github.dispatch.return_value = {}
            github.list_runners.return_value = ()
            self.post(
                "/v1/dispatch",
                self.dispatch_body("device"),
                {"Authorization": "Bearer dispatch-token"},
            )
        self.assertEqual(self.spent(), 0, "a device dispatch was charged to the metered ceiling")


class DispatchRecordTest(unittest.TestCase):
    """The push path's hold, tested where the claim is the thing that can fail.

    Through the handler this cannot be reached: a record that is already accepted
    never gets as far as taking a hold. The claim failing with a hold in hand is
    what another consumer claiming the same delivery looks like, and that is a
    call, not a race to arrange in a server test.
    """

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        path = str(Path(self.dir.name) / "jobs.db")
        self.store = DeliveryStore(path)
        self.ledger = MeteredLedger(path)
        self.record = self.store.register(
            REPO, "delivery-9", "fp", WORKFLOW, "refs/heads/main",
            {"cindral_lane": "hosted", "cindral_target": "", "cindral_reason": "r"},
        )
        # the state a second consumer leaves behind, reached the way it does:
        # claim, then finish
        self.store.claim(REPO, "delivery-9")
        self.store.finish(REPO, "delivery-9", "accepted")

    def test_a_claim_that_fails_releases_the_hold(self) -> None:
        hold = self.ledger.reserve(REPO, "hosted", WORKFLOW, "refs/heads/main")
        self.assertEqual(self.ledger.count(0), 1)
        state, _ = dispatch_record(self.store, _github(), self.record, self.ledger, hold)
        self.assertEqual(state, "duplicate")
        self.assertEqual(self.ledger.count(0), 0, "a hold outlived the dispatch it was for")

    def test_a_claim_that_succeeds_keeps_the_hold_and_names_it(self) -> None:
        record = self.store.register(
            REPO, "delivery-10", "fp2", WORKFLOW, "refs/heads/main",
            {"cindral_lane": "hosted", "cindral_target": "", "cindral_reason": "r"},
        )
        hold = self.ledger.reserve(REPO, "hosted")
        state, _ = dispatch_record(self.store, _github(), record, self.ledger, hold)
        self.assertEqual(state, "accepted")
        self.assertEqual(self.ledger.count(0), 1, "the run exists, so the spend does")
        with self.ledger._connect() as connection:
            row = connection.execute("SELECT * FROM metered_dispatches WHERE id = ?", (hold,)).fetchone()
        self.assertEqual(row["repository"], REPO)
        self.assertEqual(row["workflow"], WORKFLOW, "a month nobody can read cannot be reconciled")

    def test_a_dispatch_github_refused_releases_the_hold(self) -> None:
        record = self.store.register(
            REPO, "delivery-11", "fp3", WORKFLOW, "refs/heads/main",
            {"cindral_lane": "hosted", "cindral_target": "", "cindral_reason": "r"},
        )
        hold = self.ledger.reserve(REPO, "hosted")
        github = _github(error=GitHubDispatchError("rejected", status_code=422))
        state, _ = dispatch_record(self.store, github, record, self.ledger, hold)
        self.assertEqual(state, "rejected")
        self.assertEqual(self.ledger.count(0), 0, "a refused dispatch was charged")

    def test_an_answer_that_may_have_started_a_run_keeps_the_hold(self) -> None:
        # a 5xx means a run may exist. Releasing would book a run nobody pays for,
        # and the ceiling is the one place that must err towards counting
        record = self.store.register(
            REPO, "delivery-12", "fp4", WORKFLOW, "refs/heads/main",
            {"cindral_lane": "hosted", "cindral_target": "", "cindral_reason": "r"},
        )
        hold = self.ledger.reserve(REPO, "hosted")
        github = _github(error=GitHubDispatchError("gateway", status_code=502))
        state, _ = dispatch_record(self.store, github, record, self.ledger, hold)
        self.assertEqual(state, "uncertain")
        self.assertEqual(self.ledger.count(0), 1, "a possible run was given back")


def _github(error: Exception | None = None):
    from unittest.mock import Mock
    github = Mock()
    if error is None:
        github.dispatch.return_value = {}
    else:
        github.dispatch.side_effect = error
    return github


class RecoveredDeliveryTest(unittest.TestCase):
    """A delivery the broker replays after a restart is a metered dispatch.

    Without a hold it is a hosted run the ceiling never saw, and the recovery
    loop is the one place that dispatches without going through a request.
    """

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        path = str(Path(self.dir.name) / "jobs.db")
        self.store = DeliveryStore(path)
        self.ledger = MeteredLedger(path)
        # wired through attach_budget, because the hold is taken by the policy
        # and an unwired policy holds nothing
        stub = type("Server", (), {})()
        stub.policy = replace(Policy.load("config/policy.toml"), minute_budget=35.0)
        stub.metered = self.ledger
        stub.deliveries = None
        attach_budget(stub, self.ledger)
        self.policy = stub.policy
        self.record = self.store.register(
            REPO, "delivery-recovered", "fp", WORKFLOW, "refs/heads/main",
            {"cindral_lane": "hosted", "cindral_target": "", "cindral_reason": "r"},
        )
        # a delivery the broker registered and never attempted, which is what
        # the recovery loop replays: ready, not attempting
        self.assertEqual(self.store.get(REPO, "delivery-recovered").state, "ready")

    def test_a_recovered_dispatch_holds_the_budget(self) -> None:
        self.assertEqual(self.ledger.count(0), 0)
        recover_ready(self.store, _github(), self.policy, ledger=self.ledger)
        self.assertEqual(self.ledger.count(0), 1, "a recovered dispatch was not charged")
        self.assertEqual(self.store.get(REPO, "delivery-recovered").state, "accepted")

    def test_a_recovered_dispatch_waits_when_the_budget_is_spent(self) -> None:
        # refused, and left ready so the next pass tries again when there is room
        for _ in range(3):
            self.ledger.reserve(REPO, "hosted")
        self.assertEqual(self.ledger.count(0), 3)
        recover_ready(self.store, _github(), self.policy, ledger=self.ledger)
        self.assertEqual(self.ledger.count(0), 3, "a refused recovery still charged the month")
        self.assertEqual(self.store.get(REPO, "delivery-recovered").state, "ready")

    def test_a_recovery_that_fails_releases_its_hold(self) -> None:
        # the delivery stays ready, so the next pass tries again. A hold stranded
        # on every pass would spend the month on dispatches that never ran
        from unittest.mock import patch
        with patch.object(self.store, "claim", side_effect=sqlite3.Error("database is locked")):
            recover_ready(self.store, _github(), self.policy, ledger=self.ledger)
        self.assertEqual(self.ledger.count(0), 0, "a failed replay kept its hold")
        self.assertEqual(self.store.get(REPO, "delivery-recovered").state, "ready")

    def test_a_recovered_dispatch_on_a_free_lane_holds_nothing(self) -> None:
        store = DeliveryStore(str(Path(self.dir.name) / "other.db"))
        record = store.register(
            REPO, "delivery-device", "fp", WORKFLOW, "refs/heads/main",
            {"cindral_lane": "device", "cindral_target": "desktop", "cindral_reason": "r"},
        )
        recover_ready(store, _github(), self.policy, ledger=self.ledger)
        self.assertEqual(self.ledger.count(0), 0, "a device dispatch was charged to hosted")


class LedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = MeteredLedger(str(Path(self.dir.name) / "jobs.db"))

    def test_a_dispatch_is_counted_from_the_moment_it_was_accepted(self) -> None:
        self.ledger.reserve(REPO, "hosted", WORKFLOW, "main", now=INSTANT)
        self.assertEqual(self.ledger.count(0), 1)

    def test_a_dispatch_from_a_previous_month_is_not_counted(self) -> None:
        self.ledger.reserve(REPO, "hosted", now=month_start(INSTANT) - 1)
        self.ledger.reserve(REPO, "hosted", now=INSTANT)
        self.assertEqual(self.ledger.count(month_start(INSTANT)), 1)

    def test_the_lane_is_part_of_the_count(self) -> None:
        for lane in ("hosted", "hosted", "device", "burst", "fallback"):
            self.ledger.reserve(REPO, lane, now=INSTANT)
        self.assertEqual(self.ledger.count(0, "hosted"), 2)
        self.assertEqual(self.ledger.count(0, "device"), 1)

    def test_the_repository_is_stored_the_way_github_writes_it(self) -> None:
        self.ledger.reserve("Example-Org/Cindral", "hosted", now=INSTANT)
        with self.ledger._connect() as connection:
            row = connection.execute("SELECT repository FROM metered_dispatches").fetchone()
        self.assertEqual(row["repository"], "example-org/cindral")


if __name__ == "__main__":
    unittest.main()
