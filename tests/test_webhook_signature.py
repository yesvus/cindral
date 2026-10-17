"""The webhook signature, and what a wrong one is answered with.

Split out because a signed body is checked before anything else happens, and a
test of the check that has to stand up a server to reach it is a test of the
server.
"""
import hashlib
import hmac
import unittest

from cindral.webhook import sign, verify_signature

from test_webhook import SECRET, push_body


class SignatureTest(unittest.TestCase):
    def test_valid_signature_is_accepted(self) -> None:
        body = push_body()
        self.assertTrue(verify_signature(SECRET, body, sign(SECRET, body)))

    def test_wrong_secret_is_rejected(self) -> None:
        body = push_body()
        self.assertFalse(verify_signature("other", body, sign(SECRET, body)))

    def test_tampered_body_is_rejected(self) -> None:
        body = push_body()
        header = sign(SECRET, body)
        self.assertFalse(verify_signature(SECRET, body + b" ", header))

    def test_missing_header_is_rejected(self) -> None:
        self.assertFalse(verify_signature(SECRET, push_body(), None))

    def test_garbage_header_is_rejected(self) -> None:
        self.assertFalse(verify_signature(SECRET, push_body(), "sha256=nothex"))

    def test_signature_matches_github_algorithm(self) -> None:
        body = b'{"zen":"x"}'
        expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.assertEqual(sign(SECRET, body), expected)
