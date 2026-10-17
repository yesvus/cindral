"""Tests for the artifact upload a device performs around a run.

An artifact is what a run produced and a reader downloads, so the device side
is about when it is sent, what it costs when it fails, and that the archive it
leaves behind is not left on the device.
"""
import io
import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from unittest.mock import patch

from cindral.agent import CindralClient, CindralError
from cindral.artifact_client import ArtifactClientError
from cindral.artifact_client import CindralArtifactClient
from cindral.models import ExecutionResult

from test_agent import FakeClient, job


class Response:
    def __init__(self, body=b"", status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class ArtifactClient(FakeClient):
    """The agent's own fake plus the archive request it makes around a run."""

    def __init__(self, jobs=(), fail_upload=False):
        super().__init__(jobs)
        self.uploads: list[tuple[str, str, str]] = []
        self.fail_upload = fail_upload

    def upload_artifacts(self, job_id, device, archive):
        self.uploads.append((job_id, device, archive))
        if self.fail_upload:
            raise CindralError("artifact upload failed with HTTP 503")
        return {"stored": True}


class ArtifactAgentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name) / "job-1.tar"
        self.archive.write_bytes(b"artifacts/report.txt")
        self.notes: list[str] = []

    def executor_returning(self, archive: str):
        def run(_job, _cancel):
            return ExecutionResult(exit_code=0, log="", steps=[], artifacts=archive)

        return run

    def agent(self, archive: str, client=None):
        from cindral.agent import Agent

        return Agent(
            client or ArtifactClient([job()]),
            "server",
            self.executor_returning(archive),
            log=self.notes.append,
        )

    def test_uploads_the_archive_before_reporting_the_run(self) -> None:
        # a reader who opens the run as soon as it finishes must already find
        # the output, so the archive goes out ahead of the verdict
        order: list[str] = []

        class Recording(ArtifactClient):
            def upload_artifacts(self, *args):
                order.append("upload")
                return super().upload_artifacts(*args)

            def report(self, *args, **kwargs):
                order.append("report")
                return super().report(*args, **kwargs)

        self.agent(str(self.archive), Recording([job()])).run_once()
        self.assertEqual(order, ["upload", "report"])

    def test_removes_the_local_archive_once_it_is_uploaded(self) -> None:
        # the device is a laptop with a disk, and an archive per run that
        # nothing reclaims is a disk the runs fill
        self.agent(str(self.archive)).run_once()
        self.assertFalse(self.archive.exists())

    def test_a_run_with_no_artifact_directory_uploads_nothing(self) -> None:
        client = ArtifactClient([job()])
        self.agent("", client).run_once()
        self.assertEqual(client.uploads, [])
        self.assertEqual(client.reports, [("job-1", "server", 0)])

    def test_a_failed_upload_still_reports_the_run(self) -> None:
        # a run's verdict is its exit code, and a broker that refused the
        # upload must not turn a green run into a lost one
        client = ArtifactClient([job()], fail_upload=True)
        self.agent(str(self.archive), client).run_once()
        self.assertEqual(client.reports, [("job-1", "server", 0)])
        self.assertEqual(client.losses, [])
        self.assertIn("artifacts were not uploaded", "".join(self.notes))
        # the archive is still removed, so a failed upload does not leave it
        self.assertFalse(self.archive.exists())

    def test_a_failed_upload_of_a_lost_run_still_reports_the_loss(self) -> None:
        from cindral.agent import Agent

        def raise_executor(_job, _cancel):
            raise RuntimeError("docker is not running")

        client = ArtifactClient([job()], fail_upload=True)
        Agent(client, "server", raise_executor, log=self.notes.append).run_once()
        self.assertEqual(client.losses, [("job-1", "server", "RuntimeError: docker is not running")])

    def test_an_executor_returning_only_an_exit_code_uploads_nothing(self) -> None:
        from cindral.agent import Agent

        client = ArtifactClient([job()])
        Agent(client, "server", lambda _job, _cancel: 0).run_once()
        self.assertEqual(client.uploads, [])


class ArtifactClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name) / "job-1.tar"
        self.archive.write_bytes(b"artifacts/report.txt")
        self.client = CindralClient("https://relay.example/", "token")

    @patch("cindral.artifact_client.urllib.request.urlopen")
    def test_sends_the_archive_as_its_own_request(self, urlopen) -> None:
        urlopen.return_value = Response(json.dumps({"stored": True}).encode())
        result = CindralArtifactClient(self.client).upload("job-1", "server", str(self.archive))
        self.assertTrue(result["stored"])
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://relay.example/v1/jobs/job-1/artifacts")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.headers["Authorization"], "Bearer token")
        self.assertEqual(request.headers["X-cindral-device"], "server")
        self.assertEqual(request.headers["Content-length"], str(len(b"artifacts/report.txt")))
        self.assertEqual(request.headers["Content-type"], "application/x-tar")

    @patch("cindral.artifact_client.urllib.request.urlopen")
    def test_an_existing_archive_is_not_an_error(self, urlopen) -> None:
        # a retried attempt of the same run uploads the same archive again,
        # which the broker answers without storing a second copy
        urlopen.return_value = Response(json.dumps({"stored": True}).encode(), 200)
        self.assertTrue(CindralArtifactClient(self.client).upload("job-1", "server", str(self.archive))["stored"])

    @patch("cindral.artifact_client.urllib.request.urlopen")
    def test_http_errors_become_artifact_client_errors(self, urlopen) -> None:
        from urllib.error import HTTPError

        urlopen.side_effect = HTTPError("url", 413, "too large", {}, io.BytesIO(b"nope"))
        with self.assertRaises(ArtifactClientError):
            CindralArtifactClient(self.client).upload("job-1", "server", str(self.archive))

    @patch("cindral.artifact_client.urllib.request.urlopen")
    def test_a_transport_failure_becomes_an_artifact_client_error(self, urlopen) -> None:
        urlopen.side_effect = OSError("connection reset")
        with self.assertRaises(ArtifactClientError):
            CindralArtifactClient(self.client).upload("job-1", "server", str(self.archive))

    @patch("cindral.artifact_client.urllib.request.urlopen")
    def test_a_malformed_reply_becomes_an_artifact_client_error(self, urlopen) -> None:
        urlopen.return_value = Response(b"not json")
        with self.assertRaises(ArtifactClientError):
            CindralArtifactClient(self.client).upload("job-1", "server", str(self.archive))

    @patch("cindral.artifact_client.urllib.request.urlopen")
    def test_refuses_an_archive_over_the_size_the_broker_will_take(self, urlopen) -> None:
        # the broker refuses this one anyway, and spending a device's bandwidth
        # to learn that is a refusal it could predict
        with patch("cindral.artifact_client.MAX_ARTIFACT_BYTES", 4):
            with self.assertRaises(ArtifactClientError):
                CindralArtifactClient(self.client).upload("job-1", "server", str(self.archive))
        urlopen.assert_not_called()

    def test_closes_the_archive_when_the_request_raises(self) -> None:
        from urllib.error import HTTPError

        with patch("cindral.artifact_client.urllib.request.urlopen") as urlopen:
            urlopen.side_effect = HTTPError("url", 500, "boom", {}, io.BytesIO(b""))
            with self.assertRaises(ArtifactClientError):
                CindralArtifactClient(self.client).upload("job-1", "server", str(self.archive))
        # a leaked handle on a device is a handle the agent process keeps for
        # the length of its life
        self.assertTrue(self.archive.exists())


class AgentArtifactFailureTest(unittest.TestCase):
    """A run that passed stays passed whatever happened to its output."""

    def setUp(self) -> None:
        self.notes: list[str] = []
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_an_archive_that_cannot_be_read_is_reported_not_raised(self) -> None:
        from cindral.agent import Agent

        class Gone(ArtifactClient):
            def upload_artifacts(self, *_args):
                raise OSError("no such file")

        client = Gone([job()])
        missing = str(Path(self.tmp.name) / "gone.tar")
        agent = Agent(client, "server", lambda _j, _c: ExecutionResult(0, artifacts=missing), log=self.notes.append)
        self.assertTrue(agent.run_once())
        self.assertIn("artifacts were not uploaded", "".join(self.notes))
        self.assertEqual(client.reports, [("job-1", "server", 0)])

    def test_the_lease_is_still_renewed_while_the_archive_is_uploaded(self) -> None:
        """The upload and the report happen inside the lease, not after it.

        The heartbeat renews every third of the lease and used to be stopped when
        the executor returned, which left the upload and the report sharing only
        the tail of what was left. A 64 MiB upload on a slow uplink is minutes,
        so the lease lapses there, the run is reclaimed, and a second device
        starts the same work while the first is still holding the archive. Both
        devices were doing exactly the right thing.

        So the heartbeat has to still be running when the archive is sent and when
        the verdict is reported, and the only way to see that is to ask at those
        two points.
        """
        from cindral.agent import Agent
        from cindral.heartbeats import LeaseHeartbeat

        def executor(_spec, cancel):
            cancel.wait(0.05)
            return ExecutionResult(0, artifacts="x.tar")

        client = ArtifactClient([job()])
        seen: list[tuple[str, bool]] = []
        with mock.patch.object(LeaseHeartbeat, "stop", autospec=True) as stop:
            client.upload_artifacts = lambda *a: seen.append(("upload", stop.called))
            client.report = lambda *a, **k: seen.append(("report", stop.called))
            self.assertTrue(Agent(client, "server", executor=executor, lease_seconds=3).run_once())
        self.assertEqual(
            seen, [("upload", False), ("report", False)],
            "the lease was no longer being renewed by the time the archive was sent",
        )

    def test_a_lost_lease_leaves_no_archive_on_the_device(self) -> None:
        """A packed archive that is never sent has to be removed anyway.

        The packer writes beside the run's workspace rather than inside it, so
        the workspace cleanup cannot reach it and the upload is the only thing
        that would. A lease lost between the pack and the send skips the upload
        entirely, so every such run left up to the cap on the device for ever,
        and nothing sweeps them. The reclaim also drops the broker's copy, since
        that run is executing again on another device.

        The sibling test passes a path that does not exist, so it cannot see the
        leak. This one writes a real file and looks for it afterwards.
        """
        from cindral.agent import Agent

        class Losing(ArtifactClient):
            def renew(self, *_args):
                raise CindralError("lease lost")

        with TemporaryDirectory() as work:
            archive = Path(work) / "job-1.tar"
            archive.write_bytes(b"an archive nobody will read")

            def executor(_spec, cancel):
                cancel.wait(4)
                return ExecutionResult(0, artifacts=str(archive))

            client = Losing([job()])
            client.upload_artifacts = lambda *_a: self.fail("a lost lease must not upload")
            self.assertTrue(Agent(client, "server", executor=executor, lease_seconds=3).run_once())
            self.assertFalse(archive.exists(), "the archive stayed on the device")
            self.assertEqual(client.uploads, [])
            self.assertEqual(client.reports, [])

    def test_a_lease_lost_during_the_upload_stops_the_report(self) -> None:
        """The upload is the last window before the verdict, so it is checked.

        The archive is sent before the report, and the send is a network round
        trip the lease can lapse inside. Reporting after that is asking the broker
        to accept a run from a device that no longer holds it, and the broker is
        right to refuse; the device would then sit waiting for a verdict that is
        never coming while another device runs the same commit.

        So the check is asked again after the upload, and the test moves the lease
        away at exactly that point rather than at a moment it hopes is right.
        """
        from cindral import agent as agent_module
        from cindral import heartbeats
        from cindral.agent import Agent

        with TemporaryDirectory() as work:
            archive = Path(work) / "job-1.tar"
            archive.write_bytes(b"artifacts/report.txt")

            def executor(_spec, cancel):
                cancel.wait(0.05)
                return ExecutionResult(0, artifacts=str(archive))

            client = ArtifactClient([job()])
            agent = Agent(client, "server", executor=executor, lease_seconds=3)
            made: list[object] = []
            real = heartbeats.LeaseHeartbeat

            class Tracked(real):  # the run builds the heartbeat itself
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, **kwargs)
                    made.append(self)

            real_upload = client.upload_artifacts

            def lose_the_lease(*args):
                real_upload(*args)
                made[0].lost.set()  # the lease moved on while the body was in flight

            client.upload_artifacts = lose_the_lease
            with mock.patch.object(agent_module, "LeaseHeartbeat", Tracked):
                self.assertTrue(agent.run_once())
            self.assertEqual(len(client.uploads), 1, "the upload never happened")
            self.assertEqual(
                client.reports, [],
                "the run reported a verdict after losing the lease during the upload",
            )
            self.assertFalse(archive.exists(), "the archive stayed on the device")

    def test_a_lost_lease_uploads_nothing(self) -> None:
        # the lease moved on, so the broker will refuse both the upload and the
        # report; sending the archive anyway would attach one run's output to
        # whichever attempt holds the run next
        from cindral.agent import Agent

        class Losing(ArtifactClient):
            def renew(self, *_args):
                raise CindralError("lease lost")

        client = Losing([job()])
        client.upload_artifacts = lambda *_a: self.fail("a lost lease must not upload")

        def executor(_spec, cancel):
            cancel.wait(4)
            return ExecutionResult(0, artifacts="x.tar")

        self.assertTrue(Agent(client, "server", executor=executor, lease_seconds=3).run_once())
        self.assertEqual(client.uploads, [])
        self.assertEqual(client.reports, [])


if __name__ == "__main__":
    unittest.main()
