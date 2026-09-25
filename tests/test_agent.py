import io
import json
import time
import unittest
from unittest.mock import patch

from cindral.agent import Agent, JobSpec, CindralClient, CindralError


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


def job(job_id="job-1", command=("pnpm", "ci")):
    return JobSpec(
        id=job_id,
        repository="yesvus/leotron-yesvus",
        sha="abc123",
        ref="main",
        command=command,
        timeout=3600,
    )


class FakeClient:
    def __init__(self, jobs=()):
        self._jobs = list(jobs)
        self.claims = []
        self.renewals = []
        self.reports = []

    def claim(self, device, labels=(), lease_seconds=300):
        self.claims.append((device, tuple(labels), lease_seconds))
        return self._jobs.pop(0) if self._jobs else None

    def renew(self, job_id, device, lease_seconds=300):
        self.renewals.append((job_id, device))
        return True

    def report(self, job_id, device, exit_code):
        self.reports.append((job_id, device, exit_code))
        return {"status": "success" if exit_code == 0 else "failure"}


class CindralClientTest(unittest.TestCase):
    @patch("cindral.agent.urllib.request.urlopen")
    def test_claim_parses_the_job(self, urlopen) -> None:
        urlopen.return_value = Response(
            json.dumps(
                {
                    "job": {
                        "id": "job-1",
                        "repository": "yesvus/leotron-yesvus",
                        "sha": "abc123",
                        "ref": "main",
                        "command": ["pnpm", "ci"],
                        "timeout": 3600,
                    }
                }
            ).encode()
        )
        claimed = CindralClient("https://relay.example/", "token").claim("gurbet", ["arm64"])
        self.assertEqual(claimed.sha, "abc123")
        self.assertEqual(claimed.command, ("pnpm", "ci"))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://relay.example/v1/jobs/claim")
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("Bearer token", request.headers["Authorization"])

    @patch("cindral.agent.urllib.request.urlopen")
    def test_claim_returns_none_when_the_queue_is_empty(self, urlopen) -> None:
        urlopen.return_value = Response(b"", 204)
        self.assertIsNone(CindralClient("https://relay.example", "token").claim("gurbet"))

    @patch("cindral.agent.urllib.request.urlopen")
    def test_http_errors_become_relay_errors(self, urlopen) -> None:
        from urllib.error import HTTPError

        urlopen.side_effect = HTTPError("url", 401, "unauthorized", {}, io.BytesIO(b"nope"))
        with self.assertRaises(CindralError):
            CindralClient("https://relay.example", "token").claim("gurbet")

    @patch("cindral.agent.urllib.request.urlopen")
    def test_report_sends_the_exit_code(self, urlopen) -> None:
        urlopen.return_value = Response(json.dumps({"status": "failure"}).encode())
        payload = CindralClient("https://relay.example", "token").report("job-1", "gurbet", 7)
        self.assertEqual(payload["status"], "failure")
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent["exit_code"], 7)


class AgentTest(unittest.TestCase):
    def test_run_once_returns_false_without_a_job(self) -> None:
        client = FakeClient()
        executed = []
        agent = Agent(client, "gurbet", executor=lambda spec: executed.append(spec) or 0)
        self.assertFalse(agent.run_once())
        self.assertEqual(executed, [])
        self.assertEqual(client.reports, [])

    def test_run_once_executes_and_reports(self) -> None:
        client = FakeClient([job()])
        seen = []

        def executor(spec):
            seen.append(spec)
            return 0

        agent = Agent(client, "gurbet", executor=executor, labels=("arm64",))
        self.assertTrue(agent.run_once())
        self.assertEqual(seen[0].id, "job-1")
        self.assertEqual(client.claims[0][1], ("arm64",))
        self.assertEqual(client.reports, [("job-1", "gurbet", 0)])

    def test_executor_failure_reports_a_nonzero_exit(self) -> None:
        client = FakeClient([job()])

        def executor(spec):
            raise RuntimeError("docker is not running")

        self.assertTrue(Agent(client, "gurbet", executor=executor).run_once())
        self.assertEqual(client.reports, [("job-1", "gurbet", 1)])

    def test_lease_is_renewed_while_the_job_runs(self) -> None:
        client = FakeClient([job()])

        def executor(spec):
            time.sleep(1.2)
            return 0

        Agent(client, "gurbet", executor=executor, lease_seconds=3).run_once()
        self.assertTrue(client.renewals)
        self.assertEqual(client.renewals[0], ("job-1", "gurbet"))


if __name__ == "__main__":
    unittest.main()
