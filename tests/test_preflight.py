"""Tests for the capability check that gates a device's claims."""
import threading
import time
import unittest
from unittest import mock

from cindral.preflight import (
    PREFLIGHT_INTERVAL_SECONDS,
    PREFLIGHT_TIMEOUT_SECONDS,
    DockerPreflight,
)

REFUSAL = (
    "permission denied while trying to connect to the docker API at "
    "unix:///var/run/docker.sock"
)
# a real `docker version` against a daemon this process cannot use exits 1 and
# puts the reason on one line, which is what the check has to name for an operator


class FakeCapture:
    """Stands in for the executor's short-command helper, one answer per call."""

    def __init__(self, answers=((0, "29.1.3"),)):
        self.answers = list(answers)
        self.calls = []
        self.lock = threading.Lock()
        self.release = threading.Event()

    def __call__(self, argv):
        with self.lock:
            self.calls.append(argv)
            answer = self.answers.pop(0) if self.answers else (0, "29.1.3")
        if answer == "hang":
            self.release.wait(10)
            return 0, "29.1.3"
        return answer

    def count(self):
        with self.lock:
            return len(self.calls)


def preflight(capture, note=None, **kwargs):
    return DockerPreflight("docker", capture, note=note, **kwargs)


class DockerPreflightTest(unittest.TestCase):
    def _docker_on_path(self):
        patcher = mock.patch("cindral.preflight.shutil.which", return_value="/usr/bin/docker")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_check_asks_the_daemon_for_its_version(self) -> None:
        """The answer has to come from the daemon, not from the CLI on disk."""
        self._docker_on_path()
        capture = FakeCapture()
        self.assertTrue(preflight(capture).ready())
        self.assertEqual(capture.calls, [["docker", "version", "--format", "{.Server.Version}"]])

    def test_a_missing_binary_is_named_and_claims_nothing(self) -> None:
        capture = FakeCapture()
        notes = []
        check = preflight(capture, note=notes.append)
        with mock.patch("cindral.preflight.shutil.which", return_value=None):
            self.assertFalse(check.ready())
        self.assertEqual(capture.calls, [], "a missing CLI has nothing to run")
        self.assertIn("docker is not on PATH", notes[0])
        self.assertIn("claiming no jobs", notes[0])

    def test_a_daemon_that_refuses_is_named_with_its_reason(self) -> None:
        """A socket this process cannot open is the whole failure here."""
        self._docker_on_path()
        notes = []
        check = preflight(FakeCapture([(1, REFUSAL)]), note=notes.append)
        self.assertFalse(check.ready())
        self.assertIn("docker cannot reach its daemon (exit 1)", notes[0])
        self.assertIn("permission denied", notes[0])
        self.assertIn("claiming no jobs", notes[0])

    def test_a_refusal_is_reported_once_and_not_on_every_poll(self) -> None:
        self._docker_on_path()
        notes = []
        check = preflight(FakeCapture([(1, REFUSAL)]), note=notes.append, interval=30)
        for _ in range(5):
            self.assertFalse(check.ready())
        self.assertEqual(len(notes), 1, f"the failure repeated into the log: {notes}")

    def test_it_recovers_once_the_daemon_answers(self) -> None:
        self._docker_on_path()
        notes = []
        capture = FakeCapture([(1, REFUSAL), (0, "29.1.3")])
        check = preflight(capture, note=notes.append, interval=0)
        self.assertFalse(check.ready())
        self.assertTrue(check.ready())
        self.assertIn("permission denied", notes[0])
        self.assertEqual(notes[1], "cindral: docker answers, claiming jobs")
        self.assertEqual(capture.count(), 2)

    def test_a_check_that_never_answers_costs_a_turn_and_not_the_loop(self) -> None:
        """A hung docker CLI must not hold the claim loop, nor say so every poll."""
        self._docker_on_path()
        notes = []
        capture = FakeCapture(["hang"])
        check = preflight(capture, note=notes.append, interval=0, timeout=0.2)
        started = time.monotonic()
        try:
            self.assertFalse(check.ready())
            self.assertLess(time.monotonic() - started, 2, "the claim loop waited on the check")
            self.assertEqual(capture.count(), 1, "each poll started another hung probe")
        finally:
            capture.release.set()
        self.assertIn("did not answer within 0.2s", notes[0])
        self.assertIn("claiming no jobs", notes[0])

    def test_a_hung_probe_is_replaced_once_the_interval_passes(self) -> None:
        self._docker_on_path()
        capture = FakeCapture(["hang", (0, "29.1.3")])
        check = preflight(capture, interval=0.05, timeout=0.2)
        self.addCleanup(capture.release.set)
        self.assertFalse(check.ready())
        capture.release.set()
        deadline = time.monotonic() + 5
        while not check.ready() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(check.ready(), "the device never recovered after the daemon came back")
        self.assertEqual(capture.count(), 2, "a hung probe was never replaced")

    def test_a_probe_that_raises_is_reported_as_a_failure(self) -> None:
        self._docker_on_path()
        notes = []

        def capture(argv):
            raise OSError("cannot start docker")

        check = preflight(capture, note=notes.append)
        self.assertFalse(check.ready())
        self.assertIn("OSError", notes[0])
        self.assertIn("claiming no jobs", notes[0])

    def test_a_healthy_first_check_says_so_once(self) -> None:
        self._docker_on_path()
        notes = []
        check = preflight(FakeCapture(), note=notes.append, interval=30)
        for _ in range(3):
            self.assertTrue(check.ready())
        self.assertEqual(notes, ["cindral: docker answers, claiming jobs"])

    def test_one_probe_serves_every_caller_inside_the_interval(self) -> None:
        """Slot threads poll together, and one poll must not cost a docker call."""
        self._docker_on_path()
        capture = FakeCapture()
        check = preflight(capture, interval=30)
        answers = []

        def poll():
            answers.append(check.ready())

        threads = [threading.Thread(target=poll) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(4)
        self.assertEqual(answers, [True] * 4)
        self.assertEqual(capture.count(), 1)

    def test_the_cadence_defaults_to_a_minute_and_the_deadline_to_ten_seconds(self) -> None:
        self.assertEqual(PREFLIGHT_INTERVAL_SECONDS, 60.0)
        self.assertEqual(PREFLIGHT_TIMEOUT_SECONDS, 10.0)


if __name__ == "__main__":
    unittest.main()
