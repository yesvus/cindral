import unittest
from cindral.status_text import failure_description
from cindral.service import CindralHandler, CindralServer, parse_status_target_url

STATUS_TARGET = "https://panel.example.com/api/jobs/{job_id}"


class FailureDescriptionTest(unittest.TestCase):
    def test_a_run_without_markers_reports_the_exit_code(self) -> None:
        self.assertEqual(failure_description(1, ""), "device pool run failed (exit 1)")

    def test_the_last_step_wins(self) -> None:
        log = "cindral: step 1: pnpm install\nok\ncindral: step 2: pnpm run ci\nboom\n"
        self.assertEqual(
            failure_description(2, log), "device pool run failed (exit 2): step 2: pnpm run ci"
        )

    def test_a_failure_before_the_first_step_names_the_diagnostic(self) -> None:
        log = "cindral: job abc example-org/app@1234\ncindral: dockerfile not found: ci/Dockerfile\n"
        self.assertEqual(
            failure_description(1, log),
            "device pool run failed (exit 1): dockerfile not found: ci/Dockerfile",
        )

    def test_the_description_stays_within_the_status_limit(self) -> None:
        log = f"cindral: step 1: {'echo ' * 200}\n"
        self.assertLessEqual(len(failure_description(1, log)), 140)


class StatusTargetUrlTest(unittest.TestCase):
    def test_an_unset_template_links_to_nothing(self) -> None:
        self.assertEqual(parse_status_target_url(""), "")
        server = CindralServer(("127.0.0.1", 0), CindralHandler)
        self.addCleanup(server.server_close)
        server.status_target_url = ""
        self.assertEqual(server.job_target_url("job-123"), "")

    def test_a_template_must_name_the_job(self) -> None:
        with self.assertRaises(ValueError):
            parse_status_target_url("https://panel.example.com/jobs")

    def test_a_template_must_be_a_plain_http_url(self) -> None:
        with self.assertRaises(ValueError):
            parse_status_target_url("panel.example.com/jobs/{job_id}")
        with self.assertRaises(ValueError):
            parse_status_target_url("https://user:pass@panel.example.com/jobs/{job_id}")

    def test_the_job_id_is_escaped_into_the_link(self) -> None:
        server = CindralServer(("127.0.0.1", 0), CindralHandler)
        self.addCleanup(server.server_close)
        server.status_target_url = STATUS_TARGET
        self.assertEqual(
            server.job_target_url("a/../b"),
            "https://panel.example.com/api/jobs/a%2F..%2Fb",
        )


if __name__ == "__main__":
    unittest.main()
