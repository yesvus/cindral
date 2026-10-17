"""The threads an agent runs alongside its own work, and a stalled broker.

Both threads are about a fact the broker cannot otherwise know: that a device is
still there, and that a job it is running still belongs to it. The stalled broker
is here because it is the same property under failure: a device must notice a
broker that stopped answering.
"""
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

from cindral.agent import (
    MAX_CONCURRENCY,
    MAX_HEARTBEAT_SECONDS,
    MIN_IDLE_SECONDS,
    ONLINE_WINDOW_SECONDS,
    Agent,
    CindralClient,
    CindralError,
)

from cindral.heartbeats import LeaseHeartbeat

from test_agent import CLIENT_TIMEOUT, JOB_BODY, FakeClient, StalledHandler, job


def _await(done, timeout=10):
    deadline = time.time() + timeout
    while not done() and time.time() < deadline:
        time.sleep(0.02)
    return done()


class LeaseHeartbeatTest(unittest.TestCase):
    def _beat(self, client, lease=3):
        cancel = threading.Event()
        beat = LeaseHeartbeat(client, "job-1", "server", lease, cancel)
        beat.start()
        self.addCleanup(beat.stop)
        return beat, cancel

    def test_a_refused_renewal_cancels_the_executor(self) -> None:
        """409 and 404 are answers: the lease is gone and someone else may hold it."""
        client = FakeClient()
        client.renew = lambda *a, **k: (_ for _ in ()).throw(
            CindralError("relay request failed with HTTP 409", status=409)
        )
        beat, cancel = self._beat(client)
        self.assertTrue(cancel.wait(10), "a refused lease did not cancel the executor")
        self.assertTrue(beat.lost.is_set())

    def test_a_renewal_that_never_arrived_does_not_cancel_the_executor(self) -> None:
        """One timeout is not a lost lease, and cancelling on it threw work away.

        The lease is still ours for another interval and the next tick comes before
        it lapses. Cancelling here stopped a long build that was making progress and
        left the job `running` with nobody reporting on it, so the broker reclaimed it
        and a second device ran the same work: the reclaim counter climbed, the
        attempt count incremented, and nothing reported an error.
        """
        client = FakeClient()
        calls: list[int] = []

        def flaky(*_a, **_k):
            calls.append(1)
            if len(calls) == 1:
                raise CindralError("relay request failed: timed out")
            return {}

        client.renew = flaky
        beat, cancel = self._beat(client)
        self.assertTrue(
            beat._stopped.wait(0) or cancel.wait(6) is False,
            "a single failed renewal must not cancel a running job",
        )
        self.assertGreaterEqual(len(calls), 2, "the renewal was not retried")
        self.assertFalse(beat.lost.is_set())
        beat.stop()

    def test_a_renewal_keeps_failing_gives_up_when_the_lease_could_have_lapsed(self) -> None:
        """Retrying forever would race a second device onto the same work."""
        client = FakeClient()
        client.renew = lambda *a, **k: (_ for _ in ()).throw(
            CindralError("relay request failed: timed out")
        )
        beat, cancel = self._beat(client, lease=3)
        self.assertTrue(cancel.wait(15), "a lease nothing could renew never gave up")
        self.assertTrue(beat.lost.is_set())


class AgentHeartbeatTest(unittest.TestCase):
    def test_serve_forever_announces_the_device_and_repeats(self) -> None:
        client = FakeClient()
        agent = Agent(client, "server", executor=lambda spec, cancel: 0, heartbeat_seconds=0.05)
        heartbeat = agent.start_heartbeat()
        self.addCleanup(heartbeat.stop)
        self.addCleanup(agent.stop)
        deadline = time.time() + 4
        while len(client.heartbeats) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(len(client.heartbeats), 2)
        self.assertEqual(client.heartbeats[0], ("server", (), 1))

    def test_heartbeat_reports_the_configured_slots(self) -> None:
        client = FakeClient()
        agent = Agent(
            client,
            "server",
            executor=lambda spec, cancel: 0,
            labels=("arm64",),
            max_concurrency=4,
            heartbeat_seconds=0.05,
        )
        agent.announce()
        self.assertEqual(client.heartbeats, [("server", ("arm64",), 4)])

    def test_a_failing_heartbeat_does_not_stop_claiming(self) -> None:
        client = FakeClient([job()], fail_heartbeat=True)
        agent = Agent(client, "server", executor=lambda spec, cancel: 0)
        with self.assertRaises(CindralError):
            agent.announce()
        # the device keeps working against a broker that has no heartbeat route
        self.assertTrue(agent.run_once())
        self.assertEqual(client.reports, [("job-1", "server", 0)])

    def test_heartbeat_failure_is_reported_once_until_it_recovers(self) -> None:
        client = FakeClient(fail_heartbeat=True)
        notes = []
        agent = Agent(
            client,
            "server",
            executor=lambda spec, cancel: 0,
            heartbeat_seconds=0.05,
            log=notes.append,
        )
        heartbeat = agent.start_heartbeat()
        self.addCleanup(heartbeat.stop)
        self.addCleanup(agent.stop)
        deadline = time.time() + 4
        while not notes and time.time() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)
        self.assertEqual(len(notes), 1)
        self.assertIn("heartbeat failed, claiming continues", notes[0])
        client._fail_heartbeat = False
        deadline = time.time() + 4
        while len(notes) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(notes[1], "cindral: heartbeat recovered")


class AgentConcurrencyTest(unittest.TestCase):
    def test_rejects_a_slot_count_below_one(self) -> None:
        with self.assertRaises(ValueError):
            Agent(FakeClient(), "server", executor=lambda spec, cancel: 0, max_concurrency=0)

    def test_rejects_a_non_positive_idle_wait(self) -> None:
        """A zero or negative wait returns at once and spins the claim endpoint."""
        for idle in (0, -1, -15.0):
            with self.subTest(idle=idle), self.assertRaises(ValueError):
                Agent(FakeClient(), "server", executor=lambda spec, cancel: 0, idle_seconds=idle)

    def test_rejects_an_idle_wait_too_short_to_poll_sensibly(self) -> None:
        """A sub-0.1s wait turns an empty queue into a claim request per wait."""
        self.assertEqual(MIN_IDLE_SECONDS, 0.1)
        for idle in (0.001, 0.01, 0.05, 0.099):
            with self.subTest(idle=idle), self.assertRaises(ValueError):
                Agent(FakeClient(), "server", executor=lambda spec, cancel: 0, idle_seconds=idle)
        Agent(FakeClient(), "server", executor=lambda spec, cancel: 0, idle_seconds=MIN_IDLE_SECONDS)

    def test_rejects_a_non_finite_idle_wait(self) -> None:
        """`inf` passes a bare positivity test and then overflows `Event.wait`."""
        for idle in (float("inf"), float("nan"), float("-inf")):
            with self.subTest(idle=idle), self.assertRaises(ValueError):
                Agent(FakeClient(), "server", executor=lambda spec, cancel: 0, idle_seconds=idle)

    def test_rejects_a_non_positive_lease(self) -> None:
        for lease in (0, -300):
            with self.subTest(lease=lease), self.assertRaises(ValueError):
                Agent(FakeClient(), "server", executor=lambda spec, cancel: 0, lease_seconds=lease)

    def test_rejects_a_slot_count_past_the_broker_limit(self) -> None:
        """The broker refuses such a heartbeat, which costs the device its entry."""
        Agent(FakeClient(), "server", executor=lambda spec, cancel: 0, max_concurrency=MAX_CONCURRENCY)
        with self.assertRaises(ValueError):
            Agent(FakeClient(), "server", executor=lambda spec, cancel: 0, max_concurrency=MAX_CONCURRENCY + 1)

    def test_rejects_a_heartbeat_slower_than_the_online_window(self) -> None:
        executor = lambda spec, cancel: 0
        Agent(FakeClient(), "server", executor=executor, heartbeat_seconds=MAX_HEARTBEAT_SECONDS)
        for interval in (MAX_HEARTBEAT_SECONDS + 0.1, ONLINE_WINDOW_SECONDS, 0, -1):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                Agent(FakeClient(), "server", executor=executor, heartbeat_seconds=interval)

    def test_runs_claimed_jobs_concurrently_up_to_the_slot_count(self) -> None:
        jobs = [job(f"job-{index}") for index in range(4)]
        client = FakeClient(jobs)
        running = threading.Semaphore(0)
        release = threading.Event()
        peak = 0
        active = 0
        guard = threading.Lock()

        def executor(spec, cancel):
            nonlocal peak, active
            with guard:
                active += 1
                peak = max(peak, active)
            running.release()
            release.wait(4)
            with guard:
                active -= 1
            return 0

        agent = Agent(
            client,
            "server",
            executor=executor,
            max_concurrency=2,
            idle_seconds=MIN_IDLE_SECONDS,
            heartbeat_seconds=30,
        )
        worker = threading.Thread(target=agent.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(agent.stop)
        self.addCleanup(release.set)
        for _ in range(2):
            self.assertTrue(running.acquire(timeout=4))
        # both slots are busy, so no third claim reaches the executor
        time.sleep(0.2)
        self.assertEqual(len(client.claims), 2)
        self.assertEqual(peak, 2)
        release.set()
        agent.stop()
        worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(client.reports), 2)

    def test_serve_forever_claims_until_the_queue_empties(self) -> None:
        client = FakeClient([job("job-1"), job("job-2")])
        agent = Agent(
            client,
            "server",
            executor=lambda spec, cancel: 0,
            max_concurrency=2,
            idle_seconds=MIN_IDLE_SECONDS,
            heartbeat_seconds=30,
        )
        worker = threading.Thread(target=agent.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(agent.stop)
        deadline = time.time() + 4
        while len(client.reports) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(sorted(client.reports), [("job-1", "server", 0), ("job-2", "server", 0)])
        agent.stop()
        worker.join(4)
        self.assertFalse(worker.is_alive())


class StalledBrokerTest(unittest.TestCase):
    """A transport failure must cost a beat or a claim, never a thread.

    A reply that stops or hangs up part way through raises out of the request
    rather than returning, and the thread that let it through is gone for the
    rest of the process: a heartbeat thread that dies leaves the device taking
    jobs unannounced, and a dead slot is capacity the heartbeat keeps promising.
    """

    def _relay(self, script=None):
        """A real broker on a loopback port, answering `script` by request path."""
        server = ThreadingHTTPServer(("127.0.0.1", 0), StalledHandler)
        server.script = {path: list(replies) for path, replies in (script or {}).items()}
        server.paths = []
        server.lock = threading.Lock()
        relay = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        relay.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        return CindralClient(url, "token", timeout=CLIENT_TIMEOUT), server

    def test_a_stalled_or_hung_up_reply_becomes_a_cindral_error(self) -> None:
        for broken in ("stall", "cut"):
            with self.subTest(broken=broken):
                client, _ = self._relay({"/v1/jobs/claim": [broken]})
                with self.assertRaises(CindralError) as raised:
                    client.claim("server")
                self.assertIn("relay request failed", str(raised.exception))

    def test_the_heartbeat_thread_survives_a_stalled_beat(self) -> None:
        client, server = self._relay({"/v1/agents/heartbeat": ["stall"]})
        notes = []
        agent = Agent(
            client, "server", executor=lambda spec, cancel: 0, heartbeat_seconds=0.05, log=notes.append
        )
        heartbeat = agent.start_heartbeat()
        self.addCleanup(heartbeat.stop)
        self.addCleanup(agent.stop)
        self.assertTrue(
            _await(lambda: any("heartbeat recovered" in note for note in notes)),
            f"the beat never recovered, notes were {notes}",
        )
        self.assertIn("cindral: heartbeat failed, claiming continues: relay request failed", notes[0])
        self.assertGreaterEqual(server.paths.count("/v1/agents/heartbeat"), 2)
        self.assertTrue(heartbeat.is_alive())

    def test_the_heartbeat_thread_survives_an_unexpected_announce_failure(self) -> None:
        class OneBadBeat(FakeClient):
            def __init__(self) -> None:
                super().__init__()
                self.beats = 0

            def heartbeat(self, device, labels=(), max_concurrency=1):
                self.beats += 1
                if self.beats == 1:
                    raise ValueError("relay bug")
                return super().heartbeat(device, labels, max_concurrency)

        notes = []
        agent = Agent(
            OneBadBeat(), "server", executor=lambda spec, cancel: 0, heartbeat_seconds=0.05, log=notes.append
        )
        heartbeat = agent.start_heartbeat()
        self.addCleanup(heartbeat.stop)
        self.addCleanup(agent.stop)
        self.assertTrue(
            _await(lambda: any("heartbeat recovered" in note for note in notes)),
            f"the beat never recovered, notes were {notes}",
        )
        self.assertTrue(heartbeat.is_alive())

    def test_a_stalled_claim_does_not_cost_a_slot(self) -> None:
        client, server = self._relay({"/v1/jobs/claim": ["stall", "stall"]})
        agent = Agent(
            client, "server", executor=lambda spec, cancel: 0, max_concurrency=2,
            idle_seconds=MIN_IDLE_SECONDS, heartbeat_seconds=30,
        )
        worker = threading.Thread(target=agent.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(agent.stop)
        claims = lambda: server.paths.count("/v1/jobs/claim")
        # both stalls landing means both slots died outright, and the device kept
        # advertising two slots it no longer had
        self.assertTrue(_await(lambda: claims() >= 8), f"claiming stopped at {claims()} claims")
        self.assertTrue(worker.is_alive())
        agent.stop()
        worker.join(10)
        self.assertFalse(worker.is_alive())

    def test_a_single_slot_agent_keeps_claiming_after_a_stalled_claim(self) -> None:
        client, server = self._relay({"/v1/jobs/claim": ["stall", JOB_BODY]})
        agent = Agent(
            client, "server", executor=lambda spec, cancel: 0,
            idle_seconds=MIN_IDLE_SECONDS, heartbeat_seconds=30,
        )
        worker = threading.Thread(target=agent.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(agent.stop)
        reported = _await(lambda: "/v1/jobs/job-1/report" in server.paths)
        self.assertTrue(reported, "the agent stopped working after a stalled claim")
        self.assertTrue(worker.is_alive())
        agent.stop()
        worker.join(10)
        self.assertFalse(worker.is_alive())

    def test_a_slot_that_raises_unexpectedly_is_restarted_and_says_so(self) -> None:
        class OneBadClaim(FakeClient):
            def __init__(self, jobs) -> None:
                super().__init__(jobs)
                self.attempts = 0

            def claim(self, device, labels=(), lease_seconds=300):
                self.attempts += 1
                if self.attempts == 1:
                    raise ValueError("relay bug")
                return super().claim(device, labels, lease_seconds)

        notes = []
        client = OneBadClaim([job()])
        agent = Agent(
            client, "server", executor=lambda spec, cancel: 0, idle_seconds=MIN_IDLE_SECONDS,
            heartbeat_seconds=30, log=notes.append,
        )
        worker = threading.Thread(target=agent.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(agent.stop)
        self.assertTrue(_await(lambda: client.reports), "the slot did not come back for its job")
        self.assertEqual(client.reports, [("job-1", "server", 0)])
        self.assertTrue(
            any("slot restarted" in note and "relay bug" in note for note in notes),
            f"the restart was not reported, notes were {notes}",
        )
        agent.stop()
        worker.join(10)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()


if __name__ == '__main__':
    unittest.main()
