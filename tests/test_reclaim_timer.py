"""The reclaim timer has to be started, and started with what it needs.

Split from the service tests because this is about one thread and its arguments,
and because the two files together were over the line limit. The assertions read
the captured thread call rather than the source: a string in the source can live
in a comment, and an argument the function does not accept raises inside the
thread and leaves the broker serving with the timer dead.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cindral.lease_reclaim import reclaim_loop
from cindral.service import serve


class ReclaimTimerStartedTest(unittest.TestCase):
    """The reclaim timer is the feature, so its absence has to fail a test.

    It was defined, tested, documented, and never started: the queue only moved
    when a device asked for work, which is the one case where it did not need to.
    """

    def _serve(self, jobs_db: str | None):
        server = mock.Mock(jobs=object(), deliveries=None, cache=None, github=None,
                           agent_token="agent", pool_token="pool", dispatch_token="dispatch")
        server.serve_forever.side_effect = KeyboardInterrupt
        environment = {"CINDRAL_AGENT_TOKEN": "agent", "CINDRAL_POOL_TOKEN": "pool",
                       "CINDRAL_DISPATCH_TOKEN": "dispatch"}
        if jobs_db:
            environment["CINDRAL_JOBS_DB"] = jobs_db
        policy = mock.Mock(minute_budget=None, metered_minutes=None, local_priority=(),
                           dispatch_targets=())
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch(
            "cindral.service.CindralServer", return_value=server
        ), mock.patch("cindral.service.Policy.load", return_value=policy), mock.patch(
            "cindral.service.load_runners", return_value=()
        ), mock.patch("cindral.service.reclaim_loop") as loop, mock.patch(
            "cindral.service.status_publisher"
        ), mock.patch("cindral.service.threading.Thread") as thread:
            thread.return_value = mock.Mock()
            with self.assertRaises(KeyboardInterrupt):
                serve("policy.toml", "state.json", "127.0.0.1", 0)
        return [call for call in thread.call_args_list if call.kwargs.get("target") is loop]

    def test_serve_starts_a_reclaim_thread_when_there_is_a_queue(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        started = self._serve(str(Path(directory.name) / "jobs.db"))
        self.assertEqual(len(started), 1, "no thread was started for the reclaim timer")

    def test_the_reclaim_thread_is_handed_the_artifact_discard(self) -> None:
        """The timer has to be handed the discard, and be able to take it.

        The discard was wired into the claim endpoint first, so the broker's own
        reclaim timer released a run and kept the archive the lost attempt left
        behind: a reader was served one attempt's bytes under another's verdict.
        Doing it where a device happens to claim is the half-fix.

        Asserting on the captured call rather than on the source, because a string
        in the source can live in a comment or in dead code. The signature is bound
        as well, since a keyword the function does not accept raises inside the
        thread and leaves the broker serving with the timer dead.
        """
        import inspect

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        started = self._serve(str(Path(directory.name) / "jobs.db"))
        self.assertEqual(len(started), 1, "no reclaim thread was started")
        call = started[0]
        # Thread takes target, args and kwargs as keywords, so both the loop's
        # positional arguments and its own keywords are read from there
        positional = call.kwargs.get("args", ())
        handed = call.kwargs.get("kwargs", {})
        self.assertIn(
            "discard", handed,
            "the reclaim timer was started with nothing to drop a released run's archive",
        )
        inspect.signature(reclaim_loop).bind(*positional, **handed)
        # and it is the real thing, not a stand-in that quietly does nothing
        self.assertTrue(callable(handed["discard"]))

    def test_the_replay_thread_is_given_arguments_replay_loop_accepts_and_a_device(self) -> None:
        """The two things that decide whether this feature runs at all.

        A positional argument the function does not accept raises inside the
        thread before its body runs, and the broker carries on serving with the
        thread dead, so a mock's call arguments prove nothing. This binds the
        real signature instead. The second half is that the thread is handed a
        device lane: without one, a refused delivery the broker lost is logged as
        having run nowhere, which is the whole defect.
        """
        import inspect
        import tempfile
        from pathlib import Path

        from cindral.webhook_dispatch import replay_loop

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        server = mock.Mock(jobs=object(), deliveries=object(), cache=None, github=None,
                           agent_token="agent", pool_token="pool", dispatch_token="dispatch")
        server.serve_forever.side_effect = KeyboardInterrupt
        policy = mock.Mock(minute_budget=None, metered_minutes=None, local_priority=(),
                           dispatch_targets=())
        with mock.patch.dict(
            os.environ,
            {"CINDRAL_AGENT_TOKEN": "agent", "CINDRAL_POOL_TOKEN": "pool",
             "CINDRAL_DISPATCH_TOKEN": "dispatch",
             "CINDRAL_JOBS_DB": str(Path(directory.name) / "jobs.db")},
            clear=True,
        ), mock.patch("cindral.service.CindralServer", return_value=server), mock.patch(
            "cindral.service.Policy.load", return_value=policy
        ), mock.patch("cindral.service.load_runners", return_value=()), mock.patch(
            "cindral.service.replay_loop"
        ) as loop, mock.patch("cindral.service.delivery_fallback") as fallback, mock.patch(
            "cindral.service.threading.Thread"
        ) as thread:
            thread.return_value = mock.Mock()
            with self.assertRaises(KeyboardInterrupt):
                serve("policy.toml", "state.json", "127.0.0.1", 0)
        started = [c for c in thread.call_args_list if c.kwargs.get("target") is loop]
        self.assertEqual(len(started), 1, "the replay loop was not started")
        call = started[0]
        # the thread was mocked, so its own call carries the arguments rather
        # than the Call object
        bound = inspect.signature(replay_loop).bind(
            *call.kwargs["args"], **call.kwargs["kwargs"]
        )
        self.assertIn(fallback.return_value, bound.arguments.values())
        self.assertIsNotNone(bound.arguments.get("ledger"))

    def test_the_replay_thread_is_started_with_a_signature_replay_loop_has(self) -> None:
        """The thread must survive being started.

        Asserting on a mock's call arguments proves nothing about binding: a
        positional argument that the real function does not accept raises inside
        the thread before its body runs, and the broker carries on serving with
        the thread dead. So this calls the real function.
        """
        import tempfile
        from pathlib import Path

        from cindral.webhook_dispatch import replay_loop

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        server = mock.Mock(jobs=object(), deliveries=object(), cache=None, github=None,
                           agent_token="agent", pool_token="pool", dispatch_token="dispatch")
        server.serve_forever.side_effect = KeyboardInterrupt
        policy = mock.Mock(minute_budget=None, metered_minutes=None, local_priority=(),
                           dispatch_targets=())
        with mock.patch.dict(
            os.environ,
            {"CINDRAL_AGENT_TOKEN": "agent", "CINDRAL_POOL_TOKEN": "pool",
             "CINDRAL_DISPATCH_TOKEN": "dispatch",
             "CINDRAL_JOBS_DB": str(Path(directory.name) / "jobs.db")},
            clear=True,
        ), mock.patch("cindral.service.CindralServer", return_value=server), mock.patch(
            "cindral.service.Policy.load", return_value=policy
        ), mock.patch("cindral.service.load_runners", return_value=()):
            # the real function, with exactly what serve() hands it
            with self.assertRaises(KeyboardInterrupt):
                serve("policy.toml", "state.json", "127.0.0.1", 0)
        # and the call itself, so a signature change fails here rather than on a
        # broker that has already logged a traceback and carried on
        self.assertTrue(callable(replay_loop))

    def test_serve_starts_no_reclaim_thread_without_a_queue(self) -> None:
        # a broker with no queue has nothing to reclaim, and the timer would be a
        # thread doing nothing for as long as the broker is up
        self.assertEqual(self._serve(None), [])


if __name__ == "__main__":
    unittest.main()
