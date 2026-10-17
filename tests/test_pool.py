import tempfile
import unittest
from pathlib import Path

from cindral.jobs import AGENT_RETENTION_SECONDS, JobStore
from cindral.models import Runner
from cindral.pool import AGENT_ONLINE_SECONDS, pool_snapshot, render_metrics, store_metrics


class PoolSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = JobStore(str(Path(self._tmp.name) / "jobs.db"))

    def snapshot(self, runners=(), now=None, include_recent=True):
        connection = self.store._connect()
        self.addCleanup(connection.close)
        return pool_snapshot(connection, runners, now, include_recent)

    def insert_agent(self, device: str, last_seen: float) -> None:
        connection = self.store._connect()
        try:
            connection.execute(
                "INSERT OR REPLACE INTO agents (device, labels, max_concurrency, last_seen)"
                " VALUES (?, ?, ?, ?)",
                (device, "[]", 2, last_seen),
            )
        finally:
            connection.close()

    def agent_devices(self) -> list[str]:
        connection = self.store._connect()
        try:
            return sorted(row["device"] for row in connection.execute("SELECT device FROM agents"))
        finally:
            connection.close()

    def test_expired_lease_is_reported_as_pending(self) -> None:
        # a running row past its lease is awaiting reclaim, so reporting it as
        # running would put a job on a device the panel shows as idle
        self.store.enqueue("org/app", "sha1", "main", command=("ci",), now=100.0)
        self.store.claim("server", [], lease_seconds=30, now=100.0)

        live = self.snapshot((Runner("server", "online", False, ("linux",)),), now=120.0)
        self.assertEqual(live["queue_depth"]["running"], 1)
        self.assertEqual(live["expired_lease_count"], 0)

        runners = (Runner("server", "online", False, ("linux",)),)
        expired = self.snapshot(runners, now=140.0)
        self.assertEqual(expired["queue_depth"]["running"], 0)
        self.assertEqual(expired["queue_depth"]["pending"], 1)
        self.assertEqual(expired["expired_lease_count"], 1)
        self.assertFalse(expired["devices"][0]["busy"])
        self.assertIsNone(expired["devices"][0]["current_lease"])

    def test_old_pending_job_without_agents_signals_stall(self) -> None:
        self.store.enqueue("org/app", "sha1", "main", now=100.0)
        young = self.snapshot(now=399.0)
        old = self.snapshot(now=400.0)
        self.assertIsNone(young["stalled_reason"])
        self.assertEqual(old["stalled_reason"], "no_agents")
        self.assertIn('cindral_pool_stalled{reason="no_agents"} 1', render_metrics((), old))

    def test_old_pending_job_with_free_slots_is_not_a_stall(self) -> None:
        # capacity the pool has and work it can take is a queue moving slowly,
        # not a stall. Calling it one is what sent an operator to register
        # runners the queue never needed, which costs hardware and fixes nothing.
        self.store.enqueue("org/app", "sha1", "main", now=100.0)
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=390.0)

        old = self.snapshot(now=400.0)
        self.assertEqual(old["devices"][0]["available_slots"], 2)
        self.assertIsNone(old["stalled_reason"])
        self.assertIn('cindral_pool_stalled{reason="idle_devices"} 0', render_metrics((), old))

    def test_a_pool_with_every_slot_taken_reports_capacity(self) -> None:
        # one device, one slot, a run on it and a run waiting: the queue is
        # waiting on capacity, which is the case worth naming and is the one the
        # inverted condition used to report as a healthy pool
        self.store.enqueue("org/app", "sha1", "main", now=100.0)
        self.store.enqueue("org/app", "sha2", "main", now=100.0)
        self.store.record_heartbeat("device-alpha", ["arm64"], 1, now=390.0)
        self.store.claim("device-alpha", [], lease_seconds=600, now=390.0)

        old = self.snapshot(now=400.0)
        self.assertEqual(old["devices"][0]["available_slots"], 0)
        self.assertEqual(old["stalled_reason"], "idle_devices")
        self.assertIn('cindral_pool_stalled{reason="idle_devices"} 1', render_metrics((), old))

    def test_a_run_waiting_out_a_backoff_names_the_backoff(self) -> None:
        # the queue put the run back with a future available_at and nothing is
        # going to claim it until then, so neither a device nor capacity is the
        # answer, and naming either sends the reader after the wrong one
        job = self.store.enqueue("org/app", "sha1", "main", now=100.0)
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=390.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=390.0)
        self.store.reclaim_expired(now=490.0)
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=495.0)
        self.assertEqual(self.store.get(job.id).status, "pending")

        old = self.snapshot(now=510.0)
        self.assertEqual(old["queue_depth"]["pending"], 1)
        self.assertEqual(old["stalled_reason"], "deferred")
        self.assertIn('cindral_pool_stalled{reason="deferred"} 1', render_metrics((), old))

    def test_a_run_no_device_advertises_names_the_labels(self) -> None:
        self.store.enqueue("org/app", "sha1", "main", labels=("gpu",), now=100.0)
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=390.0)

        old = self.snapshot(now=400.0)
        self.assertEqual(old["stalled_reason"], "no_eligible_device")
        self.assertIn('cindral_pool_stalled{reason="no_eligible_device"} 1', render_metrics((), old))

    def test_every_lease_on_a_device_is_listed(self) -> None:
        # a double claim stays visible instead of one lease overwriting the
        # other while the running count still counts both
        self.store.enqueue("org/a", "sha1", "main", command=("ci",), now=100.0)
        self.store.enqueue("org/b", "sha2", "main", command=("ci",), now=101.0)
        first = self.store.claim("server", [], lease_seconds=300, now=110.0)
        second = self.store.claim("server", [], lease_seconds=300, now=120.0)

        runners = (Runner("server", "online", False, ("linux",)),)
        snapshot = self.snapshot(runners, now=130.0)
        self.assertEqual(snapshot["queue_depth"]["running"], 2)
        leases = snapshot["devices"][0]["leases"]
        self.assertEqual({lease["job_id"] for lease in leases}, {first.id, second.id})
        # the soonest expiry is the one that frees the slot first
        self.assertEqual(snapshot["devices"][0]["current_lease"]["job_id"], first.id)

    def test_a_lease_device_absent_from_state_is_still_listed(self) -> None:
        self.store.enqueue("org/app", "sha1", "main", command=("ci",), now=100.0)
        self.store.claim("dynamic", [], lease_seconds=300, now=110.0)

        snapshot = self.snapshot((), now=120.0)
        self.assertEqual([d["name"] for d in snapshot["devices"]], ["dynamic"])
        self.assertTrue(snapshot["devices"][0]["busy"])

    def test_an_idle_agent_appears_with_free_slots(self) -> None:
        # a device that is connected but has nothing to run is the state an
        # operator most needs to see and previously could not
        self.store.record_heartbeat("device-alpha", ["arm64", "device"], 2, now=100.0)

        device = self.snapshot((), now=110.0)["devices"][0]
        self.assertEqual(device["name"], "device-alpha")
        self.assertEqual(device["labels"], ["arm64", "device"])
        self.assertTrue(device["agent_online"])
        self.assertEqual(device["last_seen"], 100.0)
        self.assertEqual(device["max_concurrency"], 2)
        self.assertEqual(device["active_jobs"], 0)
        self.assertEqual(device["available_slots"], 2)
        self.assertFalse(device["busy"])

    def test_a_device_with_no_agent_reports_unknown_capacity(self) -> None:
        device = self.snapshot((Runner("device-gamma", "online", False, ("linux",)),), now=110.0)["devices"][0]
        self.assertFalse(device["agent_online"])
        self.assertIsNone(device["last_seen"])
        self.assertIsNone(device["max_concurrency"])
        self.assertIsNone(device["available_slots"])
        self.assertEqual(device["active_jobs"], 0)

    def test_agent_metadata_joins_a_configured_runner_without_changing_it(self) -> None:
        self.store.record_heartbeat("server", ["arm64"], 4, now=100.0)
        runner = Runner("server", "offline", True, ("self-hosted", "ARM64"))

        devices = self.snapshot((runner,), now=110.0)["devices"]
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["status"], "offline")
        self.assertTrue(devices[0]["busy"])
        self.assertEqual(devices[0]["labels"], ["self-hosted", "ARM64"])
        self.assertTrue(devices[0]["agent_online"])
        self.assertEqual(devices[0]["max_concurrency"], 4)

    def test_active_jobs_come_from_leases_not_the_reported_count(self) -> None:
        # max_concurrency is the device's own claim; the work it holds is the
        # broker's record, so a device cannot under-report what it is running
        self.store.enqueue("org/a", "sha1", "main", command=("ci",), now=100.0)
        self.store.enqueue("org/b", "sha2", "main", command=("ci",), now=100.0)
        self.store.record_heartbeat("device-alpha", ["arm64"], 4, now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=300, now=105.0)
        self.store.claim("device-alpha", [], lease_seconds=300, now=105.0)

        device = self.snapshot((), now=110.0)["devices"][0]
        self.assertEqual(device["active_jobs"], 2)
        self.assertEqual(device["available_slots"], 2)

    def test_available_slots_never_go_negative(self) -> None:
        self.store.enqueue("org/a", "sha1", "main", command=("ci",), now=100.0)
        self.store.enqueue("org/b", "sha2", "main", command=("ci",), now=100.0)
        self.store.record_heartbeat("device-alpha", ["arm64"], 1, now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=300, now=105.0)
        self.store.claim("device-alpha", [], lease_seconds=300, now=105.0)

        device = self.snapshot((), now=110.0)["devices"][0]
        self.assertEqual(device["active_jobs"], 2)
        self.assertEqual(device["available_slots"], 0)

    def test_an_agent_that_stopped_heartbeating_is_not_online(self) -> None:
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=100.0)
        snapshot = self.snapshot((), now=100.0 + AGENT_ONLINE_SECONDS + 1)
        device = snapshot["devices"][0]
        self.assertFalse(device["agent_online"])
        # still listed, so an operator sees a device that went quiet
        self.assertEqual(device["last_seen"], 100.0)

    def test_an_agent_inside_the_window_is_online(self) -> None:
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=100.0)
        self.assertTrue(self.snapshot((), now=100.0 + AGENT_ONLINE_SECONDS)["devices"][0]["agent_online"])

    def test_an_offline_agent_offers_no_free_slots(self) -> None:
        # a device that stopped reporting takes no new work, so a positive count
        # here would send work to a host nothing is listening on
        self.store.record_heartbeat("device-alpha", ["arm64"], 4, now=100.0)

        device = self.snapshot((), now=100.0 + AGENT_ONLINE_SECONDS + 1)["devices"][0]
        self.assertFalse(device["agent_online"])
        self.assertIsNone(device["available_slots"])
        # the limit it was configured with is still a fact about the device
        self.assertEqual(device["max_concurrency"], 4)

    def test_an_offline_agent_excludes_its_slots_from_metrics(self) -> None:
        self.store.record_heartbeat("device-alpha", ["arm64"], 4, now=100.0)
        runners = (Runner("device-alpha", "online", False, ("linux",)),)
        stale = self.snapshot(runners, now=100.0 + AGENT_ONLINE_SECONDS + 1)

        text = render_metrics(runners, stale)
        self.assertIn('cindral_agent_devices{connected="true"} 0', text)
        self.assertIn('cindral_agent_devices{connected="false"} 1', text)
        self.assertIn("cindral_agent_available_slots 0", text)

    def test_a_configured_runner_holding_a_lease_is_listed_once(self) -> None:
        # a second row for the same name would report the runner as online and
        # healthy whatever state.json says, and would count its lease and slots
        # a second time in every pool gauge
        self.store.enqueue("org/app", "sha1", "main", command=("ci",), now=100.0)
        self.store.claim("device-gamma", [], lease_seconds=300, now=105.0)
        self.store.record_heartbeat("device-gamma", ["arm64"], 4, now=100.0)
        runner = Runner("device-gamma", "offline", False, ("linux",), healthy=False)

        snapshot = self.snapshot((runner,), now=110.0)
        self.assertEqual([d["name"] for d in snapshot["devices"]], ["device-gamma"])
        device = snapshot["devices"][0]
        self.assertEqual(device["status"], "offline")
        self.assertFalse(device["healthy"])
        self.assertEqual(device["active_jobs"], 1)
        self.assertEqual(device["available_slots"], 3)

        text = render_metrics((runner,), snapshot)
        self.assertIn("cindral_devices_busy 1", text)
        self.assertIn('cindral_agent_devices{connected="true"} 1', text)
        self.assertIn('cindral_agent_devices{connected="false"} 0', text)
        self.assertIn("cindral_agent_available_slots 3", text)

    def test_include_recent_skips_the_log_column(self) -> None:
        # a metrics scrape must not pay for the log of every recent job
        self.store.enqueue("org/app", "sha1", "main", command=("ci",))
        snapshot = self.snapshot(include_recent=False)
        self.assertEqual(snapshot["recent_jobs"], [])
        self.assertEqual(snapshot["queue_depth"]["pending"], 1)

    def test_a_long_log_is_truncated_to_a_tail_preview(self) -> None:
        self.store.enqueue("org/app", "sha1", "main", command=("ci",))
        job = self.store.claim("server", [], lease_seconds=300, now=100.0)
        assert job is not None
        self.store.report(
            job.id, "server", 0, now=110.0, log="x" * (64 * 1024 + 100)
        )

        recent = self.snapshot(now=120.0)["recent_jobs"][0]
        self.assertEqual(len(recent["log"]), 4000)
        self.assertTrue(recent["log_truncated"])

    def test_metrics_render_without_a_queue(self) -> None:
        text = render_metrics((Runner("server", "online", False, ()),), None)
        self.assertIn('cindral_devices{status="online"} 1', text)
        self.assertIn('cindral_devices{status="offline"} 0', text)
        self.assertNotIn("cindral_queue_depth", text)

    def test_metrics_render_device_and_queue_gauges(self) -> None:
        self.store.enqueue("org/app", "sha1", "main", command=("ci",))
        runners = (
            Runner("server", "online", False, ("linux",)),
            Runner("desktop", "offline", False, ("linux",)),
        )
        text = render_metrics(runners, self.snapshot(runners))
        self.assertIn('cindral_devices{status="online"} 1', text)
        self.assertIn('cindral_devices{status="offline"} 1', text)
        self.assertIn('cindral_queue_depth{state="pending"} 1', text)
        self.assertIn("cindral_devices_busy 0", text)

    def test_metrics_render_surfaces_expired_leases(self) -> None:
        self.store.enqueue("org/app", "sha1", "main", command=("ci",), now=100.0)
        self.store.claim("server", [], lease_seconds=30, now=100.0)
        text = render_metrics((), self.snapshot(now=140.0))
        self.assertIn("cindral_expired_leases 1", text)
        self.assertIn('cindral_queue_depth{state="pending"} 1', text)

    def test_a_lease_still_held_by_a_live_device_is_not_reported_as_reclaimable(self) -> None:
        # the grace means the run is on the device, so the panel must not call it
        # queued for another device while the reclaim would refuse to take it
        self.store.enqueue("org/app", "sha1", "main", command=("ci",), now=100.0)
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=130.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)

        snapshot = self.snapshot(now=140.0)
        self.assertEqual(snapshot["expired_lease_count"], 0)
        self.assertEqual(snapshot["queue_depth"]["running"], 1)
        self.assertEqual(snapshot["queue_depth"]["pending"], 0)

    def test_a_spent_lease_ages_as_waiting_work(self) -> None:
        # queue_depth counts a lease the reclaim can take back as waiting, so the
        # age and the reason have to read the same rows. They did not, and one
        # snapshot reported a job waiting, zero seconds old, and no stall.
        self.store.enqueue("org/app", "sha1", "main", command=("ci",), now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=110.0)

        snapshot = self.snapshot(now=600.0)
        self.assertEqual(snapshot["queue_depth"]["pending"], 1)
        self.assertEqual(snapshot["queue_depth"]["running"], 0)
        self.assertEqual(snapshot["oldest_pending_age_seconds"], 500.0)
        self.assertEqual(snapshot["stalled_reason"], "no_agents")

    def test_a_spent_lease_waiting_out_a_backoff_is_deferred(self) -> None:
        # A claimed row keeps the available_at its release wrote, so a row the
        # reclaim can take back can be inside a backoff as well. No write path
        # produces that today, and this pins it: the backoff check reads the same
        # set the queue depth counts, or a row the queue calls waiting would be
        # invisible to the reader that explains why.
        self.store.enqueue("org/app", "sha1", "main", command=("ci",), now=100.0)
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=595.0)
        self.store._write(
            lambda connection: connection.execute(
                "UPDATE jobs SET status = ?, device = ?, lease_expires = ?, available_at = ?",
                ("running", "device-alpha", 120.0, 900.0),
            )
        )

        snapshot = self.snapshot(now=600.0)
        self.assertEqual(snapshot["queue_depth"]["pending"], 1)
        self.assertEqual(snapshot["stalled_reason"], "deferred")

    def test_a_spent_lease_no_device_can_take_names_the_labels(self) -> None:
        # the label check reads the same set the queue depth counts, or a row the
        # queue calls waiting would never be checked against the labels a device
        # would have to advertise to take it
        self.store.enqueue("org/app", "sha1", "main", labels=("gpu",), now=100.0)
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=595.0)
        self.store._write(
            lambda connection: connection.execute(
                "UPDATE jobs SET status = ?, device = ?, lease_expires = ?",
                ("running", "device-alpha", 120.0),
            )
        )

        snapshot = self.snapshot(now=600.0)
        self.assertEqual(snapshot["queue_depth"]["pending"], 1)
        self.assertEqual(snapshot["stalled_reason"], "no_eligible_device")

    def test_a_takeable_spent_lease_is_not_blamed_on_a_device(self) -> None:
        # the other direction of the same read: a row the reclaim can take back is
        # checked against the labels too, so a device that could take it is not
        # told that no device can
        self.store.enqueue("org/app", "sha1", "main", now=100.0)
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=595.0)
        self.store._write(
            lambda connection: connection.execute(
                "UPDATE jobs SET status = ?, device = ?, lease_expires = ?",
                ("running", "device-alpha", 120.0),
            )
        )

        snapshot = self.snapshot(now=600.0)
        self.assertEqual(snapshot["devices"][0]["available_slots"], 2)
        self.assertIsNone(snapshot["stalled_reason"])

    def test_metrics_split_reclaims_taken_from_a_device_still_there(self) -> None:
        # a run taken back from a device that was still announcing was restarted
        # rather than lost, and only the split says which of the two it was
        self.store.enqueue("org/app", "sha1", "main", command=("ci",), now=100.0)
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=100.0)
        self.store.claim("device-alpha", [], lease_seconds=30, now=100.0)
        self.store.record_heartbeat("device-alpha", ["arm64"], 2, now=180.0)
        self.store.reclaim_expired(now=195.0)

        snapshot = self.snapshot(now=196.0)
        self.assertEqual(snapshot["reclaim_count"], 1)
        self.assertEqual(snapshot["live_device_reclaim_count"], 1)
        text = render_metrics((), snapshot)
        self.assertIn("cindral_reclaims_live_device_total 1", text)

    def test_metrics_count_connected_agents_apart_from_configured_devices(self) -> None:
        # a configured device with no agent is the usual cause of a stalled
        # queue, so the two counts must not be summed into one number
        self.store.record_heartbeat("server", ["arm64"], 2, now=100.0)
        runners = (
            Runner("server", "online", False, ("linux",)),
            Runner("desktop", "online", False, ("linux",)),
        )
        text = render_metrics(runners, self.snapshot(runners, now=110.0))
        self.assertIn('cindral_agent_devices{connected="true"} 1', text)
        self.assertIn('cindral_agent_devices{connected="false"} 1', text)
        self.assertIn("cindral_agent_available_slots 2", text)

    def test_store_metrics_renders_the_same_text(self) -> None:
        self.store.enqueue("org/app", "sha1", "main", command=("ci",))
        text = store_metrics(self.store, (Runner("server", "online", False, ()),))
        self.assertIn('cindral_queue_depth{state="pending"} 1', text)

    def test_store_metrics_counts_configured_devices_from_a_generator(self) -> None:
        # the snapshot and the render each walk the runners, so a one-shot
        # sequence would be spent before the gauges are counted and the pool
        # would report no devices at all
        runners = (
            Runner("server", "online", False, ("linux",)),
            Runner("desktop", "offline", False, ("linux",)),
        )
        text = store_metrics(self.store, (runner for runner in runners))
        self.assertIn('cindral_devices{status="online"} 1', text)
        self.assertIn('cindral_devices{status="offline"} 1', text)
        self.assertIn('cindral_agent_devices{connected="false"} 2', text)

    def test_a_snapshot_prunes_agents_past_retention(self) -> None:
        # a pool whose agents all go offline never takes the write path again,
        # so a retired device would sit in every snapshot until some unrelated
        # device heartbeated and pruned it as a side effect
        now = 1_000.0 + AGENT_RETENTION_SECONDS + 60.0
        # written straight to the table because a heartbeat prunes the stale row
        # on its own, which would hide what this test is about
        self.insert_agent("retired", now - AGENT_RETENTION_SECONDS - 86_400.0)
        self.insert_agent("device-alpha", now - 60.0)

        snapshot = self.snapshot((), now=now)
        self.assertEqual([d["name"] for d in snapshot["devices"]], ["device-alpha"])
        self.assertEqual(snapshot["devices"][0]["last_seen"], now - 60.0)
        self.assertEqual(self.agent_devices(), ["device-alpha"])


if __name__ == "__main__":
    unittest.main()
