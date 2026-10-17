"""What a device says about itself, and what the broker does with it.

A device agent is a checkout on the host, so the commit it reports is the only
place its age is visible. The broker holds no git history, so it can show the two
commits and nothing more; refusing the work is the device's job, because the
history is on the device.
"""
import unittest

from cindral.jobapi import parse_heartbeat
from cindral.jobapi import Heartbeat
from job_endpoint_harness import POOL_TOKEN, JobEndpointHarness


class AgentBuildTest(JobEndpointHarness, unittest.TestCase):
    def test_heartbeat_answers_200_without_a_build(self) -> None:
        # an older agent sends nothing, and refusing its heartbeat would take
        # every device on that version offline at once
        status, _payload = self.heartbeat()
        self.assertEqual(status, 200)


class ParseHeartbeatBuildTest(unittest.TestCase):
    def test_a_heartbeat_with_no_build_parses_to_an_empty_one(self) -> None:
        parsed = parse_heartbeat({"device": "a", "labels": [], "max_concurrency": 1})
        self.assertIsInstance(parsed, Heartbeat)
        self.assertEqual(parsed.build, {})

    def test_the_named_build_fields_are_kept(self) -> None:
        build = {"version": "0.12.0", "commit": "a" * 40, "built_at": "2026-09-29T16:13:04Z"}
        parsed = parse_heartbeat(
            {"device": "a", "labels": [], "max_concurrency": 1, "build": build}
        )
        self.assertEqual(parsed.build, build)

    def test_a_build_key_the_broker_does_not_use_is_dropped(self) -> None:
        # the panel renders these, so a device cannot put an arbitrary key in a
        # stored column
        parsed = parse_heartbeat(
            {
                "device": "a",
                "labels": [],
                "max_concurrency": 1,
                "build": {"commit": "b" * 40, "note": "<script>alert(1)</script>"},
            }
        )
        self.assertEqual(parsed.build, {"commit": "b" * 40})

    def test_a_build_that_is_not_an_object_of_strings_is_refused(self) -> None:
        for build in ({"commit": 12}, {"commit": {"sha": "x"}}, [1, 2], "a" * 40, 7, None):
            with self.subTest(build=build):
                with self.assertRaises(ValueError):
                    parse_heartbeat(
                        {"device": "a", "labels": [], "max_concurrency": 1, "build": build}
                    )


class HeartbeatBuildTest(JobEndpointHarness, unittest.TestCase):
    def test_the_build_a_device_reports_reaches_the_pool(self) -> None:
        # the broker has no git history, so the device's own commit is the only
        # thing that makes its age visible to an operator reading the pool
        build = {"version": "0.12.0", "commit": "a" * 40, "built_at": "2026-09-29T16:13:04Z"}
        status, payload = self.heartbeat({"device": "server", "labels": [], "max_concurrency": 1, "build": build})
        self.assertEqual(status, 200)
        _, snapshot = self.call("GET", "/v1/pool", token=POOL_TOKEN)
        device = next(d for d in snapshot["devices"] if d["name"] == "server")
        self.assertEqual(device["build"], build)
        self.assertTrue(device["build_known"])

    def test_a_device_that_reports_no_build_reads_as_unknown_not_as_current(self) -> None:
        # an older agent sends no build. Reading that as an up-to-date device is
        # the failure this column exists to prevent
        self.heartbeat()
        _, snapshot = self.call("GET", "/v1/pool", token=POOL_TOKEN)
        device = next(d for d in snapshot["devices"] if d["name"] == "server")
        self.assertEqual(device["build"], {})
        self.assertFalse(device["build_known"])

    def test_the_reply_carries_the_brokers_own_build(self) -> None:
        # so the agent can compare without a human reading two commits side by
        # side. The commit here is unset in a source checkout, which is exactly
        # the case that must be visible rather than silently absent
        _status, payload = self.heartbeat()
        self.assertEqual(set(payload["broker_build"]), {"version", "commit", "built_at"})

    def test_a_build_that_is_not_strings_is_refused(self) -> None:
        for build in ({"commit": 12}, {"commit": {"sha": "x"}}, [1, 2], "a" * 40):
            with self.subTest(build=build):
                status, _ = self.heartbeat(
                    {"device": "server", "labels": [], "max_concurrency": 1, "build": build}
                )
                self.assertEqual(status, 400)

    def test_a_build_field_the_broker_does_not_use_is_dropped(self) -> None:
        # the panel renders these, so a device cannot put an arbitrary key in a
        # column. Only the three named fields are stored
        status, _ = self.heartbeat(
            {
                "device": "server",
                "labels": [],
                "max_concurrency": 1,
                "build": {"commit": "b" * 40, "note": "<script>"},
            }
        )
        self.assertEqual(status, 200)
        _, snapshot = self.call("GET", "/v1/pool", token=POOL_TOKEN)
        device = next(d for d in snapshot["devices"] if d["name"] == "server")
        self.assertEqual(device["build"], {"commit": "b" * 40})


if __name__ == '__main__':
    unittest.main()
