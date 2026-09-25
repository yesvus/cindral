import json
import unittest
from unittest.mock import patch

from cindral.github import GitHubAPIError, GitHubClient, GitHubDispatchError


class Response:
    def __init__(self, body=b""):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class GitHubClientTest(unittest.TestCase):
    @patch("cindral.github.urlopen")
    def test_dispatch_uses_workflow_dispatch_api(self, urlopen) -> None:
        urlopen.return_value = Response()
        GitHubClient("token").dispatch("yesvus/waymux", "ci.yml", "main", {"cindral_lane": "fallback"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/yesvus/waymux/actions/workflows/ci.yml/dispatches")
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("Bearer token", request.headers["Authorization"])

    @patch("cindral.github.urlopen")
    def test_dispatch_surfaces_github_errors(self, urlopen) -> None:
        from urllib.error import HTTPError

        urlopen.side_effect = HTTPError("url", 422, "bad input", {}, None)
        with self.assertRaises(GitHubDispatchError):
            GitHubClient("token").dispatch("yesvus/waymux", "ci.yml", "main", {})

    @patch("cindral.github.urlopen")
    def test_workflow_lookup_uses_repository_actions_api(self, urlopen) -> None:
        urlopen.return_value = Response()
        self.assertTrue(GitHubClient("token").workflow_exists("yesvus/waymux", "cindral-dispatch.yml"))
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/yesvus/waymux/actions/workflows/cindral-dispatch.yml",
        )
        self.assertEqual(request.get_method(), "GET")

    @patch("cindral.github.urlopen")
    def test_missing_workflow_is_not_opted_in(self, urlopen) -> None:
        from urllib.error import HTTPError

        urlopen.side_effect = HTTPError("url", 404, "not found", {}, None)
        self.assertFalse(GitHubClient("token").workflow_exists("yesvus/waymux", "cindral-dispatch.yml"))

    @patch("cindral.github.urlopen")
    def test_ensure_push_webhook_creates_hook_when_missing(self, urlopen) -> None:
        urlopen.side_effect = [Response(b"[]"), Response()]
        result = GitHubClient("token").ensure_push_webhook(
            "yesvus/waymux", "https://hook.example/cindral/dispatch", "secret-value"
        )
        self.assertEqual(result, "created")
        listing, create = [call.args[0] for call in urlopen.call_args_list]
        self.assertIn("/hooks?per_page=100&page=1", listing.full_url)
        self.assertEqual(create.get_method(), "POST")
        payload = json.loads(create.data)
        self.assertEqual(payload["events"], ["push"])
        self.assertEqual(payload["config"]["secret"], "secret-value")
        self.assertEqual(payload["config"]["insecure_ssl"], "0")

    @patch("cindral.github.urlopen")
    def test_ensure_push_webhook_updates_matching_hook(self, urlopen) -> None:
        urlopen.side_effect = [
            Response(b'[{"id":42,"config":{"url":"https://hook.example/cindral/dispatch"}}]'),
            Response(),
        ]
        result = GitHubClient("token").ensure_push_webhook(
            "yesvus/waymux", "https://hook.example/cindral/dispatch", "secret-value"
        )
        self.assertEqual(result, "updated")
        request = urlopen.call_args_list[1].args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/yesvus/waymux/hooks/42")
        self.assertEqual(request.get_method(), "PATCH")

    def test_ensure_push_webhook_requires_https(self) -> None:
        with self.assertRaises(ValueError):
            GitHubClient("token").ensure_push_webhook("yesvus/waymux", "http://hook.example/cindral", "secret")

    @patch("cindral.github.urlopen")
    def test_list_runners_uses_repository_actions_api(self, urlopen) -> None:
        urlopen.return_value = Response(
            b'{"runners":[{"name":"papyrus","status":"online","busy":false,'
            b'"labels":[{"name":"self-hosted"},{"name":"device"}]}]}'
        )
        runners = GitHubClient("token").list_runners("yesvus/waymux")
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/yesvus/waymux/actions/runners?per_page=100&page=1",
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(runners[0].name, "papyrus")
        self.assertFalse(runners[0].busy)
        self.assertEqual(runners[0].labels, ("self-hosted", "device"))

    @patch("cindral.github.urlopen")
    def test_list_runners_preserves_busy_capacity(self, urlopen) -> None:
        urlopen.return_value = Response(
            b'{"runners":[{"name":"papyrus","status":"online","busy":true,'
            b'"labels":[{"name":"self-hosted"}]}]}'
        )
        runners = GitHubClient("token").list_runners("yesvus/waymux")
        self.assertTrue(runners[0].busy)

    def test_list_runners_rejects_invalid_repository(self) -> None:
        for repository in ("https://example.com/owner/repo", "../repo"):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                GitHubClient("token").list_runners(repository)

    @patch("cindral.github.urlopen")
    def test_list_runners_surfaces_github_errors(self, urlopen) -> None:
        from urllib.error import HTTPError

        urlopen.side_effect = HTTPError("url", 403, "forbidden", {}, None)
        with self.assertRaises(GitHubAPIError):
            GitHubClient("token").list_runners("yesvus/waymux")


if __name__ == "__main__":
    unittest.main()
