"""Tests for the artifact upload and download routes.

The bytes a download serves were written by the repository the run checked out, so
these are about who may read them and what a request may name, not only the round trip.
"""
import json
import time
import socket
import tempfile
import unittest
import unittest.mock
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path

from job_endpoint_harness import AGENT_TOKEN, POOL_TOKEN, REPO, JobEndpointHarness

from cindral.artifact import ArtifactStore
from cindral.artifactapi import discard_artifact

ARCHIVE = b"artifacts/nested/junit.xml\x00binary-body"


class ArtifactEndpointTest(JobEndpointHarness, unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server.artifacts = ArtifactStore(f"{self._tmp.name}/artifacts")
        self.job = self.server.jobs.enqueue(REPO, "abc123", "main")

    def claimed(self) -> str:
        return self.job.id

    def upload(self, body=ARCHIVE, token=AGENT_TOKEN, device="server", job_id=None, headers=None):
        merged = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-tar",
            "X-Cindral-Device": device,
        }
        merged.update(headers or {})
        return self.call("POST", f"/v1/jobs/{job_id or self.job.id}/artifacts", body, headers=merged)

    def download(self, job_id=None, token=POOL_TOKEN):
        return self.fetch("GET", f"/v1/jobs/{job_id or self.job.id}/artifacts", token)

    def raw_upload(self, body, declared_length):
        """A body that ends before the length it declared, as a lost upload does.

        The write side is shut down so the broker reads the end of the body
        rather than waiting for bytes a device that died will never send.
        """
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.putrequest("POST", f"/v1/jobs/{self.job.id}/artifacts")
        for name, value in (
            ("Authorization", f"Bearer {AGENT_TOKEN}"),
            ("Content-Type", "application/x-tar"),
            ("Content-Length", str(declared_length)),
            ("X-Cindral-Device", "server"),
        ):
            connection.putheader(name, value)
        connection.endheaders()
        connection.send(body)
        connection.sock.shutdown(socket.SHUT_WR)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, (json.loads(raw) if raw else {})

    def test_a_run_uploads_its_archive_and_a_reader_downloads_it(self) -> None:
        self.server.jobs.claim("server", [])
        status, payload = self.upload()
        self.assertEqual(status, 201)
        self.assertTrue(payload["stored"])
        self.assertEqual(payload["artifact"]["size"], len(ARCHIVE))
        status, body, content_type, cors = self.download()
        self.assertEqual(status, 200)
        self.assertEqual(body.encode("latin-1"), ARCHIVE)
        self.assertEqual(content_type, "application/x-tar")
        # a page a browser reaches must not be able to read a run's output
        self.assertIsNone(cors)

    def test_the_job_payload_reports_the_archive_a_reader_can_download(self) -> None:
        self.server.jobs.claim("server", [])
        self.upload()
        status, payload = self.call("GET", f"/v1/jobs/{self.job.id}", token=POOL_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload["artifacts"]["url"], f"/v1/jobs/{self.job.id}/artifacts")
        self.assertEqual(payload["artifacts"]["size"], len(ARCHIVE))

    def test_the_report_names_the_archive_uploaded_before_it(self) -> None:
        self.server.jobs.claim("server", [])
        self.upload()
        status, payload = self.call("POST", f"/v1/jobs/{self.job.id}/report", {"device": "server", "exit_code": 0})
        self.assertEqual(status, 200)
        self.assertEqual(payload["artifacts"]["size"], len(ARCHIVE))

    def test_a_run_that_produced_nothing_reads_as_no_artifact(self) -> None:
        status, payload = self.call("GET", f"/v1/jobs/{self.job.id}", token=POOL_TOKEN)
        self.assertIsNone(payload["artifacts"])
        status, _, _, _ = self.download()
        self.assertEqual(status, 404)

    def test_the_pooled_snapshot_does_not_answer_the_same_question(self) -> None:
        # the snapshot is polled and lists fifty runs at once, so it names no
        # artifact key a panel would read as "this run has none"
        self.server.jobs.claim("server", [])
        self.upload()
        _, snapshot = self.call("GET", "/v1/pool", token=POOL_TOKEN)
        self.assertNotIn("artifacts", snapshot["recent_jobs"][0])

    def test_download_refuses_an_unauthenticated_request(self) -> None:
        self.server.jobs.claim("server", [])
        self.upload()
        status, _, _, _ = self.fetch("GET", f"/v1/jobs/{self.job.id}/artifacts")
        self.assertEqual(status, 401)

    def test_download_refuses_a_token_that_is_neither(self) -> None:
        self.server.jobs.claim("server", [])
        self.upload()
        self.assertEqual(self.fetch("GET", f"/v1/jobs/{self.job.id}/artifacts", "wrong-token")[0], 401)

    def test_download_accepts_the_read_only_pool_token(self) -> None:
        self.server.jobs.claim("server", [])
        self.upload()
        # the panel holds only the read-only credential, and it must reach the
        # output of a run it can already read
        self.assertEqual(self.fetch("GET", f"/v1/jobs/{self.job.id}/artifacts", POOL_TOKEN)[0], 200)

    def test_upload_refuses_the_read_only_pool_token(self) -> None:
        self.server.jobs.claim("server", [])
        status, payload = self.upload(token=POOL_TOKEN)
        self.assertEqual(status, 401)
        self.assertEqual(self.download()[0], 404)

    def test_upload_refuses_a_device_naming_another_device(self) -> None:
        """An upload names its device in a header, which is a claim like any other.

        With one shared token, naming the device holding the lease was enough to
        write bytes against its run.
        """
        self.server.agent_token = None
        self.server.agent_tokens = {"worker-a": "token-a", "worker-b": "token-b"}
        self.server.jobs.claim("worker-a", [], lease_seconds=600)
        status, _ = self.upload(token="token-b", device="worker-a")
        self.assertEqual(status, 401)
        self.assertIsNone(self.server.artifacts.get(self.job.id))

    def test_upload_refuses_an_unauthenticated_request(self) -> None:
        self.server.jobs.claim("server", [])
        status, _ = self.call(
            "POST",
            f"/v1/jobs/{self.job.id}/artifacts",
            ARCHIVE,
            headers={"Content-Type": "application/x-tar", "X-Cindral-Device": "server"},
            authorize=False,
        )
        self.assertEqual(status, 401)

    def test_a_device_without_the_lease_cannot_attach_bytes_to_a_run(self) -> None:
        self.server.jobs.claim("server", [])
        status, payload = self.upload(device="another-device")
        self.assertEqual(status, 409)
        self.assertEqual(self.download()[0], 404)

    def _claim_as(self, device: str, lease: int = 60):
        return self.call("POST", "/v1/jobs/claim", json.dumps(
            {"device": device, "labels": [], "lease_seconds": lease}).encode(),
            headers={"Authorization": f"Bearer {AGENT_TOKEN}",
                     "Content-Type": "application/json"})

    def test_a_reclaimed_run_does_not_serve_the_attempt_that_lost_its_lease(self) -> None:
        """A reclaimed run is a new execution, not a second look at the first.

        The reclaim requeues the same job row with `attempts + 1`, so the row
        keeps its id and the archive is immutable per run. The second attempt
        therefore found the first attempt's bytes still standing, a reader
        downloaded them under the second attempt's verdict, and the second device
        was told its upload was accepted.

        Driven through the real claim route, because that is where the discard
        happens: a test that called `store.discard` itself would pass whether or
        not anything calls it.
        """
        self._claim_as("device-a", lease=1)
        first, _ = self.upload(body=b"from attempt one", device="device-a")
        self.assertEqual(first, 201)
        self.assertEqual(self.download()[1], "from attempt one")

        # the lease lapses, and device-b's next claim requeues the same row
        time.sleep(1.2)
        self._claim_as("device-b")
        self.assertEqual(self.download()[0], 404, "the stale archive survived the reclaim")

    def test_a_stale_upload_does_not_delete_the_next_attempts_archive(self) -> None:
        """The de-posed device may not delete the archive that replaced its own.

        This is the window the plain lost-lease test cannot see. There, the stale
        upload's own bytes are still the only ones stored, so dropping them looks
        right. Here the sequence is the one a real reclaim produces: the first
        attempt stores its archive, its lease lapses, the timer reclaims and
        requeues, a second device claims, executes, and uploads, and is answered
        201. Only then does the first request finish reading its body and discover
        the lease is gone.

        A discard by job id takes the second attempt's archive with it, and that
        device was told its upload succeeded, so it will never send it again. The
        run reports its own verdict and the panel shows no output, with nothing in
        the logs to say why. The ordinary case: a 300 second lease against a 64 MiB
        body on a slow uplink.

        So the drop names the digest this request stored, and a delete that matched
        nothing leaves the file alone. Asserting only that the stale device is
        refused is not enough, because it was refused correctly in the broken
        version too.
        """
        from cindral.lease_reclaim import reclaim_tick

        self._claim_as("device-a", lease=1)
        store = self.server.artifacts
        real_put = store.put
        answered: dict[str, object] = {}

        def put_then_be_replaced(*args, **kwargs):
            record, created = real_put(*args, **kwargs)
            # the lease lapses while this body was still arriving
            time.sleep(1.2)
            reclaim_tick(
                self.server.jobs, lambda job, status, target: None,
                discard=lambda job_id: discard_artifact(self.server, job_id),
            )
            # a release backs off before offering work again, and the backoff is
            # not what is under test here
            connection = self.server.jobs._connect()
            try:
                connection.execute(
                    "UPDATE jobs SET available_at = ? WHERE id = ?",
                    (time.time() - 1, self.job.id),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(self._claim_as("device-b")[0], 200, "device-b never claimed")
            # unpatch first, or the route resolves the same store and device-b's
            # own upload would recurse into this wrapper
            store.put = real_put
            answered["status"] = self.upload(
                body=b"the second attempt's archive", device="device-b"
            )[0]
            return record, created

        store.put = put_then_be_replaced
        try:
            stale_status, stale_body = self.upload(body=b"uploaded far too late", device="device-a")
        finally:
            store.put = real_put

        self.assertEqual(answered["status"], 201, "the second upload was not accepted")
        self.assertEqual(stale_status, 409, f"the de-posed device was not refused: {stale_body!r}")
        status, body = self.download()[0], self.download()[1]
        self.assertEqual(
            status, 200,
            f"the second attempt's acknowledged archive was deleted by the first: {body!r}",
        )
        self.assertEqual(body, "the second attempt's archive")

    def test_a_republished_identical_archive_belongs_to_the_second_attempt(self) -> None:
        """Identical bytes are the expected case across a retry, not a corner one.

        The packer builds each tar by hand and sets no timestamp, owner or ordering
        that varies, so two attempts of the same commit on the same device walk the
        same directory in the same order and produce byte-identical archives. A
        digest is therefore not an identity: scoping the drop on it deletes the
        second attempt's archive along with the first's, and that device was already
        answered 201 and will never send it again.

        This is the third ordering of the same window, and the one the other two
        tests cannot see, because they publish different bytes. Only the
        publication time distinguishes the two rows.

        It is the same shape as the first interleaving test with one substitution,
        which is deliberate: the difference under test is a single column of the
        delete scope, and everything else has to be held still for that to be what
        the test is about.
        """
        from cindral.lease_reclaim import reclaim_tick

        body = b"the same bytes, from the same commit, on the same device"
        self._claim_as("device-a", lease=1)
        store = self.server.artifacts
        real_put = store.put
        answered: dict[str, object] = {}

        def put_then_be_republished(*args, **kwargs):
            record, created = real_put(*args, **kwargs)
            time.sleep(1.2)
            reclaim_tick(
                self.server.jobs, lambda job, status, target: None,
                discard=lambda job_id: discard_artifact(self.server, job_id),
            )
            connection = self.server.jobs._connect()
            try:
                connection.execute(
                    "UPDATE jobs SET available_at = ? WHERE id = ?",
                    (time.time() - 1, self.job.id),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(self._claim_as("device-b")[0], 200, "device-b never claimed")
            store.put = real_put
            answered["status"] = self.upload(body=body, device="device-b")[0]
            return record, created

        store.put = put_then_be_republished
        try:
            stale_status, _ = self.upload(body=body, device="device-a")
        finally:
            store.put = real_put

        self.assertEqual(answered["status"], 201, "the second upload was not accepted")
        self.assertEqual(stale_status, 409, "the de-posed device was not refused")
        status, served = self.download()[0], self.download()[1]
        self.assertEqual(
            status, 200,
            f"byte-identical bytes from a later attempt were deleted: {served!r}",
        )
        self.assertEqual(served, body.decode())

    def test_a_second_upload_that_finds_its_lease_gone_keeps_the_standing_archive(self) -> None:
        """The other order in the same window, and the one my first fix missed.

        A run's archive is immutable, so a second upload of the same run is
        answered with the row already standing and `created` is false. The reclaim
        and a whole second upload can both land while the first body is still
        arriving, which makes this request's `put` the *second* one to run and
        hands it the other device's record.

        Scoping the discard by that record's digest buys nothing there, because the
        digest belongs to the row the delete was supposed to skip. The device
        holding it was already answered 201, so deleting it loses a run's output
        for good, and the first fix did exactly that.

        So the drop is gated on having published something. There is nothing of
        this request's own to clean up when it did not.
        """
        from cindral.lease_reclaim import reclaim_tick

        self._claim_as("device-a", lease=1)
        store = self.server.artifacts
        real_put = store.put
        answered: dict[str, object] = {}

        def put_after_being_replaced(*args, **kwargs):
            # the lease lapses, the timer reclaims, and a second device claims,
            # executes and uploads, all while this body is still arriving
            time.sleep(1.2)
            reclaim_tick(
                self.server.jobs, lambda job, status, target: None,
                discard=lambda job_id: discard_artifact(self.server, job_id),
            )
            connection = self.server.jobs._connect()
            try:
                connection.execute(
                    "UPDATE jobs SET available_at = ? WHERE id = ?",
                    (time.time() - 1, self.job.id),
                )
                connection.commit()
            finally:
                connection.close()
            self.assertEqual(self._claim_as("device-b")[0], 200, "device-b never claimed")
            store.put = real_put
            answered["status"] = self.upload(
                body=b"the second attempt's archive", device="device-b"
            )[0]
            # and only now does this request's own put run, against a row that is
            # not its own
            return real_put(*args, **kwargs)

        store.put = put_after_being_replaced
        try:
            stale_status, _ = self.upload(body=b"uploaded far too late", device="device-a")
        finally:
            store.put = real_put

        self.assertEqual(answered["status"], 201, "the second upload was not accepted")
        self.assertEqual(stale_status, 409, "the de-posed device was not refused")
        status, body = self.download()[0], self.download()[1]
        self.assertEqual(
            status, 200,
            f"the standing archive was deleted by a request that published nothing: {body!r}",
        )
        self.assertEqual(body, "the second attempt's archive")

    def test_an_upload_that_outlives_its_lease_stores_nothing(self) -> None:
        """The lease is checked before the body, so it has to be checked after too.

        Reading the body is the slow part of an upload and how slow is the sender's
        to decide, so a reclaim can land between the check and the bytes. The
        reclaim discards what the run had and requeues it, and storing afterwards
        puts the first attempt's archive back under a verdict that no longer
        belongs to it, on a run another device is already executing.

        The reclaim is driven with a clock an hour ahead rather than by waiting,
        so the window is entered deliberately instead of raced for, and the test
        says which side of the read it is on.
        """
        import time as clock

        from cindral.artifactapi import attach_artifacts

        self._claim_as("device-a", lease=300)
        store = attach_artifacts(self.server)
        real_put = store.put

        def put_then_be_reclaimed(*args, **kwargs):
            record, created = real_put(*args, **kwargs)
            # another broker's timer, with its own clock: the run is released and
            # requeued while this request is still reading the body
            self.server.jobs.reclaim_expired(now=clock.time() + 3600)
            return record, created

        store.put = put_then_be_reclaimed
        status, _ = self.upload(body=b"from an attempt that lost its lease", device="device-a")
        self.assertEqual(status, 409, "an upload that outlived its lease was accepted")
        self.assertEqual(
            self.download()[0], 404,
            "the bytes were kept after the run was reclaimed and requeued",
        )

    def test_the_reclaim_timer_drops_the_archive_too(self) -> None:
        """The timer is the path that runs when nobody is asking for work.

        The discard was wired into the claim endpoint first, which left the
        broker's own sixty-second timer releasing a run and keeping the archive
        the lost attempt produced. A reader then downloaded the first attempt's
        bytes under the second attempt's verdict, and the second device was told
        its upload was accepted. Doing it only where a device happens to claim is
        the half-fix, and it is the shape this project keeps getting wrong.
        """
        from cindral.lease_reclaim import reclaim_tick

        self._claim_as("device-a", lease=1)
        first, _ = self.upload(body=b"from attempt one", device="device-a")
        self.assertEqual(first, 201)
        self.assertEqual(self.download()[1], "from attempt one")

        time.sleep(1.2)
        released = reclaim_tick(
            self.server.jobs, lambda job, status, target: None,
            discard=lambda job_id: discard_artifact(self.server, job_id),
        )
        self.assertEqual(released, 1, "the timer released nothing to reclaim")
        self.assertEqual(
            self.download()[0], 404, "the timer released the run and kept its archive"
        )

    def test_a_run_someone_else_is_running_refuses_an_upload(self) -> None:
        other = self.server.jobs.enqueue(REPO, "def456", "main")
        self.server.jobs.claim("burst", [])
        status, _ = self.upload(job_id=other.id, device="burst")
        self.assertEqual(status, 409)

    def test_upload_for_an_unknown_run_is_not_found(self) -> None:
        status, _ = self.upload(job_id="0" * 32)
        self.assertEqual(status, 404)

    def test_refuses_an_archive_over_the_configured_size_cap(self) -> None:
        self.server.jobs.claim("server", [])
        self.server.artifacts = ArtifactStore(f"{self._tmp.name}/small", max_bytes=8)
        status, payload = self.upload(body=b"x" * 64)
        self.assertEqual(status, 413)
        self.assertIn("size limit", payload["error"])
        self.assertEqual(self.download()[0], 404)

    def test_a_run_with_no_archive_answers_not_found_rather_than_an_empty_body(self) -> None:
        self.server.jobs.claim("server", [])
        self.server.jobs.report(self.job.id, "server", 0)
        status, _, _, _ = self.download()
        self.assertEqual(status, 404)

    def test_a_short_upload_is_refused_and_stores_nothing(self) -> None:
        # a body that ends before the length it declared is a device that lost
        # the connection mid-upload, and half an archive is not a run's output
        self.server.jobs.claim("server", [])
        status, payload = self.raw_upload(b"short", declared_length=999)
        self.assertEqual(status, 400)
        self.assertIn("Content-Length", payload["error"])
        self.assertEqual(self.download()[0], 404)

    def test_a_second_upload_for_the_same_run_keeps_the_first(self) -> None:
        self.server.jobs.claim("server", [])
        self.upload(body=b"first")
        status, payload = self.upload(body=b"second")
        self.assertEqual(status, 200)
        self.assertEqual(payload["artifact"]["size"], len(b"first"))
        self.assertIn("first", self.download()[1])

    def test_an_archive_is_not_served_for_a_run_that_is_gone(self) -> None:
        # the archive outlives the run that produced it until its TTL, and
        # bytes nobody's run names are not a download
        self.server.jobs.claim("server", [])
        self.upload()
        with self.server.jobs._connect() as connection:
            connection.execute("DELETE FROM jobs WHERE id=?", (self.job.id,))
        status, _, _, _ = self.download()
        self.assertEqual(status, 404)

    def test_an_archive_is_not_served_under_another_repository_s_run(self) -> None:
        # a stored archive carries the repository it was produced for, and a
        # reader authorized for a run must not reach another repository's bytes
        # through it
        self.server.jobs.claim("server", [])
        self.upload()
        record = self.server.artifacts.get(self.job.id)
        self.assertEqual(record.repository, REPO)
        with unittest.mock.patch.object(
            type(self.server.jobs), "get", return_value=replace(self.server.jobs.get(self.job.id), repository="someone/else")
        ):
            status, _, _, _ = self.download()
        self.assertEqual(status, 404)

    def test_an_archive_larger_than_the_store_quota_is_refused(self) -> None:
        # a quota smaller than the archive evicts the row the store had just
        # written, so answering stored would name an artifact no reader can
        # fetch. The device refuses to pack one, so this is a misconfiguration.
        self.server.jobs.claim("server", [])
        self.server.artifacts = ArtifactStore(
            f"{self._tmp.name}/tiny", repository_quota=4, storage_quota=4
        )
        status, payload = self.upload(body=b"x" * 32)
        self.assertEqual(status, 507)
        self.assertIn("quota", payload["error"])
        self.assertIsNone(self.server.artifacts.get(self.job.id))
        self.assertEqual(self.download()[0], 404)

    def test_an_archive_that_fits_is_stored_and_reported(self) -> None:
        self.server.jobs.claim("server", [])
        self.server.artifacts = ArtifactStore(f"{self._tmp.name}/fits", storage_quota=4096)
        status, payload = self.upload(body=b"x" * 32)
        self.assertEqual(status, 201)
        self.assertTrue(payload["stored"])
        self.assertEqual(self.download()[2], "application/x-tar")

    def test_download_refuses_a_job_id_that_is_not_a_job_id(self) -> None:
        for job_id in ("..", "..%2F..%2Fetc%2Fpasswd", "%2e%2e%2fsecret", "not-a-uuid"):
            with self.subTest(job_id=job_id):
                self.assertIn(self.fetch("GET", f"/v1/jobs/{job_id}/artifacts", POOL_TOKEN)[0], {400, 404})

    def test_no_method_but_get_and_post_serves_the_archive(self) -> None:
        # PUT is routed to the cache by the handler, and the methods the
        # handler does not implement answer 501; what matters is that none of
        # them returns the bytes
        self.server.jobs.claim("server", [])
        self.upload()
        for method in ("PUT", "DELETE", "PATCH", "HEAD"):
            with self.subTest(method=method):
                status, body, _, _ = self.fetch(method, f"/v1/jobs/{self.job.id}/artifacts", AGENT_TOKEN)
                self.assertNotEqual(status, 200)
                self.assertNotIn("junit", body)

    def test_a_broker_with_no_artifact_store_answers_not_configured(self) -> None:
        self.server.artifacts = None
        status, body, _, _ = self.download()
        self.assertEqual(status, 503)
        self.assertIn("not configured", body)
        _, payload = self.call("GET", f"/v1/jobs/{self.job.id}", token=POOL_TOKEN)
        self.assertIsNone(payload["artifacts"])

    def test_the_download_carries_a_name_and_a_digest(self) -> None:
        self.server.jobs.claim("server", [])
        self.upload()
        status, body, _, _ = self.download()
        record = self.server.artifacts.get(self.job.id)
        self.assertEqual(record.name, f"{self.job.id}.tar")
        self.assertEqual(len(record.digest), 64)


class ArtifactStoreWiringTest(unittest.TestCase):
    """A broker wires its own store; this is the default it falls back to."""

    def setUp(self) -> None:
        from cindral.artifactapi import attach_artifacts

        self.attach = attach_artifacts
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def server(self, artifacts=...):
        class Server:
            pass

        server = Server()
        if artifacts is not ...:
            server.artifacts = artifacts
        return server

    def test_a_store_the_environment_names_is_attached_once(self) -> None:
        jobs_db = str(self.root / "jobs.db")
        server = self.server()
        store = self.attach(server, {"CINDRAL_JOBS_DB": jobs_db}.get)
        self.assertIsNotNone(store)
        # built once and kept, so a poll does not make a store per request
        self.assertIs(self.attach(server, {"CINDRAL_JOBS_DB": jobs_db}.get), store)
        self.assertEqual(store.directory, self.root / "artifacts")

    def test_the_store_directory_can_be_named(self) -> None:
        store = self.attach(
            self.server(),
            {"CINDRAL_JOBS_DB": str(self.root / "jobs.db"), "CINDRAL_ARTIFACT_DIR": str(self.root / "out")}.get,
        )
        self.assertEqual(store.directory, self.root / "out")

    def test_a_broker_with_no_jobs_database_has_no_store(self) -> None:
        # a broker that queues nothing has no run to carry output for
        self.assertIsNone(self.attach(self.server(), {}.get))

    def test_a_mistyped_limit_is_refused_by_name(self) -> None:
        # a value that is not a number is a configuration mistake, and saying so
        # is the whole difference between a broker that stops at boot and one
        # that answers 400 to every job read while /healthz stays green
        with self.assertRaises(ValueError) as raised:
            self.attach(
                self.server(),
                {
                    "CINDRAL_JOBS_DB": str(self.root / "jobs.db"),
                    "CINDRAL_ARTIFACT_TTL_SECONDS": "seven days",
                }.get,
            )
        self.assertIn("seven days", str(raised.exception))

    def test_the_broker_builds_the_store_at_startup_and_not_per_request(self) -> None:
        # Built in the request path, a bad value was a 400 on every job read and
        # nothing else. Built beside the cache, it is a boot failure in a log.
        import inspect

        from cindral import service

        self.assertIn("attach_artifacts(server)", inspect.getsource(service.serve))

    def test_a_server_that_carries_a_store_keeps_it(self) -> None:
        environment = {"CINDRAL_JOBS_DB": str(self.root / "jobs.db")}.get
        server = self.server(artifacts=None)
        self.assertIsNone(self.attach(server, environment))
        server.artifacts = ArtifactStore(self.root / "given")
        self.assertEqual(self.attach(server, environment).directory, self.root / "given")


if __name__ == "__main__":
    unittest.main()
