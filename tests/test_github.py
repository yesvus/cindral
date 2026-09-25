import unittest
from unittest.mock import patch

from runner_relay.github import GitHubAPIError, GitHubClient, GitHubDispatchError


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
    @patch("runner_relay.github.urlopen")
    def test_dispatch_uses_workflow_dispatch_api(self, urlopen) -> None:
        urlopen.return_value = Response()
        GitHubClient("token").dispatch("yesvus/waymux", "ci.yml", "main", {"relay_lane": "fallback"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/yesvus/waymux/actions/workflows/ci.yml/dispatches")
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("Bearer token", request.headers["Authorization"])

    @patch("runner_relay.github.urlopen")
    def test_dispatch_surfaces_github_errors(self, urlopen) -> None:
        from urllib.error import HTTPError

        urlopen.side_effect = HTTPError("url", 422, "bad input", {}, None)
        with self.assertRaises(GitHubDispatchError):
            GitHubClient("token").dispatch("yesvus/waymux", "ci.yml", "main", {})

    @patch("runner_relay.github.urlopen")
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
        self.assertEqual(runners[0].labels, ("self-hosted", "device"))

    def test_list_runners_rejects_invalid_repository(self) -> None:
        for repository in ("https://example.com/owner/repo", "../repo"):
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                GitHubClient("token").list_runners(repository)

    @patch("runner_relay.github.urlopen")
    def test_list_runners_surfaces_github_errors(self, urlopen) -> None:
        from urllib.error import HTTPError

        urlopen.side_effect = HTTPError("url", 403, "forbidden", {}, None)
        with self.assertRaises(GitHubAPIError):
            GitHubClient("token").list_runners("yesvus/waymux")


if __name__ == "__main__":
    unittest.main()
