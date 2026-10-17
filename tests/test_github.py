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
        GitHubClient("token").dispatch("example-org/example-app", "ci.yml", "main", {"cindral_lane": "fallback"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/example-org/example-app/actions/workflows/ci.yml/dispatches")
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("Bearer token", request.headers["Authorization"])

    @patch("cindral.github.urlopen")
    def test_dispatch_surfaces_github_errors(self, urlopen) -> None:
        from urllib.error import HTTPError

        urlopen.side_effect = HTTPError("url", 422, "bad input", {}, None)
        with self.assertRaises(GitHubDispatchError):
            GitHubClient("token").dispatch("example-org/example-app", "ci.yml", "main", {})

    @patch("cindral.github.urlopen")
    def test_workflow_lookup_uses_repository_actions_api(self, urlopen) -> None:
        urlopen.return_value = Response()
        self.assertTrue(GitHubClient("token").workflow_exists("example-org/example-app", "cindral-dispatch.yml"))
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/example-org/example-app/actions/workflows/cindral-dispatch.yml",
        )
        self.assertEqual(request.get_method(), "GET")

    @patch("cindral.github.urlopen")
    def test_missing_workflow_is_not_opted_in(self, urlopen) -> None:
        from urllib.error import HTTPError

        urlopen.side_effect = HTTPError("url", 404, "not found", {}, None)
        self.assertFalse(GitHubClient("token").workflow_exists("example-org/example-app", "cindral-dispatch.yml"))

    @patch("cindral.github.urlopen")
    def test_ensure_push_webhook_creates_hook_when_missing(self, urlopen) -> None:
        urlopen.side_effect = [Response(b"[]"), Response()]
        result = GitHubClient("token").ensure_push_webhook(
            "example-org/example-app", "https://hook.example/cindral/dispatch", "secret-value"
        )
        self.assertEqual(result, "created")
        listing, create = [call.args[0] for call in urlopen.call_args_list]
        self.assertIn("/hooks?per_page=100&page=1", listing.full_url)
        self.assertEqual(create.get_method(), "POST")
        payload = json.loads(create.data)
        self.assertEqual(payload["events"], ["push", "pull_request"])
        self.assertEqual(payload["config"]["secret"], "secret-value")
        self.assertEqual(payload["config"]["insecure_ssl"], "0")

    @patch("cindral.github.urlopen")
    def test_ensure_push_webhook_updates_matching_hook(self, urlopen) -> None:
        urlopen.side_effect = [
            Response(b'[{"id":42,"config":{"url":"https://hook.example/cindral/dispatch"}}]'),
            Response(),
        ]
        result = GitHubClient("token").ensure_push_webhook(
            "example-org/example-app", "https://hook.example/cindral/dispatch", "secret-value"
        )
        self.assertEqual(result, "updated")
        request = urlopen.call_args_list[1].args[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/example-org/example-app/hooks/42")
        self.assertEqual(request.get_method(), "PATCH")

    def test_ensure_push_webhook_requires_https(self) -> None:
        with self.assertRaises(ValueError):
            GitHubClient("token").ensure_push_webhook("example-org/example-app", "http://hook.example/cindral", "secret")

    def test_push_webhook_urls_reads_the_documented_array_event_shape(self) -> None:
        client = GitHubClient("token")
        with patch.object(
            GitHubClient,
            "_list_hooks",
            return_value=[
                {
                    "id": 1,
                    "active": True,
                    "events": ["push", "pull_request"],
                    "config": {"url": "https://hook.example/cindral/dispatch", "content_type": "json"},
                },
                {"id": 2, "events": ["issues"], "config": {"url": "https://hook.example/cindral/issues"}},
                # a hook switched off keeps its url and events, so without the
                # active check this is reported as the repository's push webhook
                # and a status report calls a repository that cannot dispatch ready
                {
                    "id": 3,
                    "active": False,
                    "events": ["push"],
                    "config": {"url": "https://hook.example/cindral/switched-off"},
                },
            ],
        ):
            urls = client.push_webhook_urls("example-org/example-app")
        self.assertEqual(urls, ("https://hook.example/cindral/dispatch",))

    def test_push_webhook_urls_rejects_a_repository_that_is_not_a_slug(self) -> None:
        with self.assertRaises(ValueError):
            GitHubClient("token").push_webhook_urls("example-org")

    @patch("cindral.github.urlopen")
    def test_list_runners_uses_repository_actions_api(self, urlopen) -> None:
        urlopen.return_value = Response(
            b'{"runners":[{"name":"desktop","status":"online","busy":false,'
            b'"labels":[{"name":"self-hosted"},{"name":"device"}]}]}'
        )
        runners = GitHubClient("token").list_runners("example-org/example-app")
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/example-org/example-app/actions/runners?per_page=100&page=1",
        )
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(runners[0].name, "desktop")
        self.assertFalse(runners[0].busy)
        self.assertEqual(runners[0].labels, ("self-hosted", "device"))

    @patch("cindral.github.urlopen")
    def test_list_runners_preserves_busy_capacity(self, urlopen) -> None:
        urlopen.return_value = Response(
            b'{"runners":[{"name":"desktop","status":"online","busy":true,'
            b'"labels":[{"name":"self-hosted"}]}]}'
        )
        runners = GitHubClient("token").list_runners("example-org/example-app")
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
            GitHubClient("token").list_runners("example-org/example-app")

    @patch("cindral.github.urlopen")
    def test_post_status_uses_statuses_api(self, urlopen) -> None:
        urlopen.return_value = Response()
        GitHubClient("token").post_status(
            "example-org/example-app", "abc123", "success", "ci passed", target_url="https://relay.example/run/1"
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.github.com/repos/example-org/example-app/statuses/abc123",
        )
        self.assertEqual(request.get_method(), "POST")
        payload = json.loads(request.data)
        self.assertEqual(payload["state"], "success")
        self.assertEqual(payload["context"], "cindral/ci")
        self.assertEqual(payload["description"], "ci passed")
        self.assertEqual(payload["target_url"], "https://relay.example/run/1")

    @patch("cindral.github.urlopen")
    def test_a_credential_provider_is_called_for_every_request(self, urlopen) -> None:
        # the broker is handed AppAuth.token, not a string: an installation
        # token expires within the hour, so it is resolved per request
        calls: list[int] = []

        def provider() -> str:
            calls.append(1)
            return f"installation-token-{len(calls)}"

        urlopen.return_value = Response()
        client = GitHubClient(provider)
        client.post_status("example-org/example-app", "abc123", "success", "ci passed")
        first = urlopen.call_args.args[0].get_header("Authorization")
        client.post_status("example-org/example-app", "abc123", "success", "ci passed")
        second = urlopen.call_args.args[0].get_header("Authorization")
        self.assertEqual(first, "Bearer installation-token-1")
        self.assertEqual(second, "Bearer installation-token-2")

    @patch("cindral.github.urlopen")
    def test_a_credential_provider_is_not_interpolated_as_its_repr(self, urlopen) -> None:
        # a provider interpolated into a header sends the string
        # "<bound method ...>" and every request comes back 401 Bad credentials,
        # which is a failure the enqueue path swallows
        urlopen.return_value = Response()
        GitHubClient(lambda: "installation-token").post_status(
            "example-org/example-app", "abc123", "pending", "queued"
        )
        header = urlopen.call_args.args[0].get_header("Authorization")
        self.assertEqual(header, "Bearer installation-token")
        self.assertNotIn("bound method", header)
        self.assertNotIn("function", header)

    @patch("cindral.github.urlopen")
    def test_a_plain_string_token_still_works(self, urlopen) -> None:
        urlopen.return_value = Response()
        GitHubClient("plain-token").post_status(
            "example-org/example-app", "abc123", "success", "ci passed"
        )
        self.assertEqual(
            urlopen.call_args.args[0].get_header("Authorization"), "Bearer plain-token"
        )

    @patch("cindral.github.urlopen")
    def test_post_status_truncates_the_description(self, urlopen) -> None:
        urlopen.return_value = Response()
        GitHubClient("token").post_status("example-org/example-app", "abc123", "failure", "x" * 200)
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(len(payload["description"]), 140)

    def test_post_status_rejects_invalid_state_and_repository(self) -> None:
        with self.assertRaises(ValueError):
            GitHubClient("token").post_status("example-org/example-app", "abc123", "done")
        with self.assertRaises(ValueError):
            GitHubClient("token").post_status("../repo", "abc123", "success")
        with self.assertRaises(ValueError):
            GitHubClient("token").post_status("example-org/example-app", "", "success")

    @patch("cindral.github.urlopen")
    def test_post_status_surfaces_github_errors(self, urlopen) -> None:
        from urllib.error import HTTPError

        urlopen.side_effect = HTTPError("url", 403, "forbidden", {}, None)
        with self.assertRaises(GitHubAPIError):
            GitHubClient("token").post_status("example-org/example-app", "abc123", "failure")


if __name__ == "__main__":
    unittest.main()
