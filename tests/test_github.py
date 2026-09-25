import unittest
from unittest.mock import patch

from runner_relay.github import GitHubClient, GitHubDispatchError


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


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


if __name__ == "__main__":
    unittest.main()
