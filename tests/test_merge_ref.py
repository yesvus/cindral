"""Which commit a pull-request run builds, decided from a lookup that can fail.

The distinction under test throughout is between *there is no test merge* and *the
lookup is broken*. They look identical to a caller that gets `None` for both, and
the consequence is that every pull request quietly stops testing the merge the
moment the lookup breaks, with nothing anywhere saying so.
"""
import io
import json
import unittest
import unittest.mock
import urllib.error

from cindral import merge_ref
from cindral.errors import GitHubAPIError
from cindral.merge_ref import (
    is_placeholder_sha,
    pull_request_merge_sha,
    run_sha_for_pull_request,
)

HEAD = "a" * 40
MERGE = "b" * 40
BASE = "c" * 40
OTHER = "d" * 40
HEADERS = {"Authorization": "Bearer t"}


def responding(body: object, code: int = 200):
    """A urlopen that answers with a body, or raises for a status code."""

    class Answer:
        def read(self) -> bytes:
            return body if isinstance(body, bytes) else json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def call(request, timeout=None):
        if code >= 400:
            raise urllib.error.HTTPError(
                request.full_url, code, "no", {}, io.BytesIO(b"")
            )
        return Answer()

    return call


class MergeLookupTest(unittest.TestCase):
    def lookup(self, body, code=200):
        with unittest.mock.patch.object(merge_ref, "urlopen", responding(body, code)):
            return pull_request_merge_sha("https://api/repos/o/r", HEADERS, "o/r", 7)

    def test_a_merge_with_this_head_as_a_parent_is_returned(self) -> None:
        self.assertEqual(self.lookup({"sha": MERGE, "parents": [{"sha": BASE}, {"sha": HEAD}]}),
                         (MERGE, (BASE, HEAD)))

    def test_no_merge_is_none_and_that_is_a_real_answer(self) -> None:
        # a pull request that conflicts, or a fork whose base moved, has no test
        # merge. That is a fact about the pull request, not a fault.
        for code in (404, 409, 422):
            with self.subTest(code=code):
                self.assertIsNone(self.lookup({}, code=code))

    def test_a_body_that_is_not_a_commit_raises(self) -> None:
        # this is the distinction. Returning None here would be indistinguishable
        # from the case above, and every pull request would stop testing its merge
        # with nothing to show for it.
        for body in ("not json at all", [], {"message": "Not Found"}, {"sha": MERGE}):
            with self.subTest(body=body):
                with self.assertRaises(GitHubAPIError):
                    self.lookup(body)

    def test_a_server_side_failure_raises(self) -> None:
        for code in (401, 403, 500, 502):
            with self.subTest(code=code):
                with self.assertRaises(GitHubAPIError):
                    self.lookup({}, code=code)


class RunShaTest(unittest.TestCase):
    def test_the_merge_is_built_when_it_has_this_head_as_a_parent(self) -> None:
        self.assertEqual(run_sha_for_pull_request(HEAD, (MERGE, (BASE, HEAD))), MERGE)

    def test_a_stale_merge_is_not_built(self) -> None:
        # the demonstrated failure: the ref is real, and it is the previous push's
        self.assertEqual(run_sha_for_pull_request(HEAD, (MERGE, (BASE, OTHER))), HEAD)

    def test_no_merge_builds_the_head(self) -> None:
        self.assertEqual(run_sha_for_pull_request(HEAD, None), HEAD)

    def test_an_answer_of_the_wrong_shape_builds_the_head(self) -> None:
        # a client that cannot answer has not earned the right to stop a run
        for answer in ((), MERGE, (None, ()), (MERGE, None), (MERGE, "nope"), object(), 7):
            with self.subTest(answer=type(answer).__name__):
                self.assertEqual(run_sha_for_pull_request(HEAD, answer), HEAD)

    def test_a_malformed_merge_sha_builds_the_head(self) -> None:
        self.assertEqual(run_sha_for_pull_request(HEAD, ("not-a-sha", (HEAD,))), HEAD)
        self.assertEqual(run_sha_for_pull_request(HEAD, ("0" * 40, (HEAD,))), HEAD)


class PlaceholderTest(unittest.TestCase):
    def test_forty_zeros_is_a_placeholder(self) -> None:
        # GitHub sends this as merge_commit_sha when there is no merge commit, or
        # when the pull request conflicts and there never will be. It is a string,
        # so a truthiness test walks past it and the run is enqueued against a
        # commit that cannot be checked out.
        self.assertTrue(is_placeholder_sha("0" * 40))
        self.assertTrue(is_placeholder_sha("0000"))

    def test_a_real_sha_is_not_a_placeholder(self) -> None:
        self.assertFalse(is_placeholder_sha(HEAD))
        self.assertFalse(is_placeholder_sha(MERGE))

    def test_an_empty_string_is_not_treated_as_a_placeholder(self) -> None:
        # an absent merge is absent, and the caller decides that with a truthiness
        # test of its own. Calling it a placeholder would conflate the two.
        self.assertFalse(is_placeholder_sha(""))


if __name__ == "__main__":
    unittest.main()
