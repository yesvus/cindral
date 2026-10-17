"""The credential shapes a device log is scrubbed of before it reaches a commit."""
import unittest

from cindral.jobrecord import Job
from cindral.redact import REDACTED, redact, redacted_job, scrubbed

GITHUB = "ghp_" + "1" * 36
PAT = "github_pat_" + "2" * 22 + "_" + "3" * 59
AWS = "AKIAIOSFODNN7EXAMPLE"
SLACK = "xoxb-1234567890-abcdefghijkl"
GITLAB = "glpat-ABCdef123456ghijkl"
NPM = "npm_" + "4" * 36
GOOGLE = "AIza" + "5" * 35
STRIPE = "sk_live_" + "6" * 24
PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEowIBAAKCAQEA7RmZ0abc\n"
    "dEFGhiJKLmnop\n"
    "-----END RSA PRIVATE KEY-----"
)


class PatternTest(unittest.TestCase):
    def test_every_credential_shape_is_removed(self) -> None:
        shapes = {
            "a GitHub token": GITHUB,
            "a GitHub fine-grained token": PAT,
            "an AWS access key id": AWS,
            "a Slack token": SLACK,
            "a GitLab token": GITLAB,
            "an npm token": NPM,
            "a Google API key": GOOGLE,
            "a Stripe live key": STRIPE,
        }
        for label, secret in shapes.items():
            with self.subTest(shape=label):
                self.assertNotIn(secret, redact(f"cloning with {secret} now"))
                self.assertIn(REDACTED, redact(f"cloning with {secret} now"))

    def test_a_private_key_is_removed_whole(self) -> None:
        # a key split across the lines the log already holds is one key, so
        # matching the body a line at a time would leave the pieces readable
        text = redact(f"writing the key\n{PRIVATE_KEY}\nwrite failed")
        self.assertNotIn("MIIEowIBAAKCAQEA7RmZ0abc", text)
        self.assertNotIn("dEFGhiJKLmnop", text)
        self.assertIn("BEGIN RSA PRIVATE KEY", text)
        self.assertIn("END RSA PRIVATE KEY", text)
        self.assertIn("write failed", text)

    def test_an_authorization_header_loses_its_value_and_keeps_its_name(self) -> None:
        line = "Authorization: Bearer ghs_0123456789abcdefghij"
        self.assertEqual(redact(line), f"Authorization: {REDACTED}")

    def test_a_named_credential_loses_its_value_and_keeps_its_name(self) -> None:
        for line, kept in (
            ("GITHUB_TOKEN=abc123def456", "GITHUB_TOKEN="),
            ("AWS_SECRET_ACCESS_KEY=wJalrX", "AWS_SECRET_ACCESS_KEY="),
            ("password: hunter2", "password: "),
            ('api_key = "abc123"', "api_key = "),
            ("export NPM_TOKEN=ghs_0123456789abcdefghij", "NPM_TOKEN="),
            ("GET /v2?access_token=xyz123456789", "access_token="),
        ):
            with self.subTest(line=line):
                text = redact(line)
                self.assertIn(kept, text)
                self.assertIn(REDACTED, text)

    def test_a_name_that_only_ends_in_a_credential_word_is_left_alone(self) -> None:
        # a scrubber that cannot be sound should not be noisy either: a step
        # printing a sort key is not printing a credential
        for line in ("monkey=3", "sort-key=name", "the key=argument is missing"):
            with self.subTest(line=line):
                self.assertEqual(redact(line), line)

    def test_a_url_password_is_removed_and_the_host_is_kept(self) -> None:
        text = redact("pushing to https://bot:hunter2@github.example/acme/app.git failed")
        self.assertNotIn("hunter2", text)
        self.assertIn("github.example/acme/app.git", text)

    def test_text_with_no_credential_in_it_is_untouched(self) -> None:
        # a run's log is mostly this, and redaction that rewrote it would make
        # every failure harder to read than it has to be
        log = "installed 1234 packages\nlint clean\n2 tests failed in 0.4s\ncommit deadbeefcafe1234"
        self.assertEqual(redact(log), log)
        self.assertFalse(scrubbed(log))

    def test_a_sha_is_not_mistaken_for_a_credential(self) -> None:
        digest = "a" * 40
        self.assertEqual(redact(f"checkout {digest} done"), f"checkout {digest} done")

    def test_scrubbed_says_what_redact_would_do(self) -> None:
        self.assertTrue(scrubbed(f"token={GITHUB}"))
        self.assertFalse(scrubbed("installed 1234 packages, nothing to remove"))


class RecordTest(unittest.TestCase):
    def record(self) -> Job:
        return Job(
            id="job-1",
            repository="acme/app",
            sha="a" * 40,
            ref="refs/heads/main",
            labels=(),
            timeout=600,
            created_at=1_750_000_000.0,
            status="failure",
            exit_code=1,
            log=f"boom {GITHUB}",
            last_error=f"RuntimeError: {GITHUB}",
            steps=({"index": 1, "name": "lint", "output": f"boom {GITHUB}", "completed": False},),
        )

    def test_every_field_a_device_writes_text_into_is_scrubbed(self) -> None:
        record = redacted_job(self.record())
        for field in (record.log, record.last_error or "", str(record.steps[0]["output"])):
            with self.subTest(field=field[:20]):
                self.assertNotIn(GITHUB, field)
                self.assertIn(REDACTED, field)

    def test_the_fields_the_record_is_derived_from_are_kept(self) -> None:
        # a scrubbed record is still the same record: the step count, the index,
        # and the completed flag are what the step table is built from
        record = redacted_job(self.record())
        self.assertEqual(record.steps[0]["name"], "lint")
        self.assertEqual(record.steps[0]["index"], 1)
        self.assertFalse(record.steps[0]["completed"])
        self.assertEqual(record.id, "job-1")

    def test_a_record_with_no_loss_leaves_the_error_alone(self) -> None:
        record = redacted_job(Job(
            id="job-1", repository="acme/app", sha="a" * 40, ref="refs/heads/main",
            labels=(), timeout=600, created_at=1_750_000_000.0, status="pending",
        ))
        self.assertIsNone(record.last_error)
        self.assertEqual(record.log, "")


if __name__ == "__main__":
    unittest.main()
