"""What the broker client will and will not put on the wire.

Split out of test_agent_surface: these are about the credential and the
transport, which is a different subject from the MCP surface that shares the file.
"""
import json
import unittest
from io import BytesIO
from unittest import mock
from urllib.error import HTTPError

from cindral import brokerclient
from cindral.brokerclient import BrokerClient, BrokerError
from cindral.repositorystate import ADAPTER_WORKFLOW

REPO = "example-org/example-app"
REF = "refs/heads/main"
class BrokerClientTest(unittest.TestCase):
    def test_a_token_is_refused_over_plaintext_to_a_remote_host(self) -> None:
        # The dispatch token authorises a dispatch to any repository in the
        # allowlist, so cleartext hands that to whoever is on the path. Loopback
        # is exempt because those bytes never leave the machine.
        client = BrokerClient("http://broker.example.com", dispatch_token="tok")
        with mock.patch.object(brokerclient, "urlopen") as opened:
            with self.assertRaisesRegex(BrokerError, "cleartext"):
                client.dispatch(REPO, ADAPTER_WORKFLOW, REF, {})
        opened.assert_not_called()

    def test_a_token_is_sent_over_https(self) -> None:
        client = BrokerClient("https://broker.example.com", dispatch_token="tok")
        with mock.patch.object(brokerclient, "urlopen") as opened:
            opened.side_effect = self._http_error(403, "ref is not permitted for this workflow")
            with self.assertRaises(BrokerError):
                client.dispatch(REPO, ADAPTER_WORKFLOW, REF, {})
        self.assertEqual(opened.call_count, 1)

    def test_loopback_still_works_over_plaintext(self) -> None:
        # the default configuration, which the guard must not forbid
        for url in ("http://127.0.0.1:8095", "http://localhost:8095"):
            with self.subTest(url=url):
                client = BrokerClient(url, dispatch_token="tok")
                with mock.patch.object(brokerclient, "urlopen") as opened:
                    opened.side_effect = self._http_error(403, "refused")
                    with self.assertRaises(BrokerError):
                        client.dispatch(REPO, ADAPTER_WORKFLOW, REF, {})
                self.assertEqual(opened.call_count, 1)

    def test_no_token_means_no_cleartext_refusal(self) -> None:
        # the guard is about the credential, not the transport: a request with
        # nothing secret still reaches the broker and gets its own answer
        client = BrokerClient("http://broker.example.com")
        with mock.patch.object(brokerclient, "urlopen") as opened:
            with self.assertRaisesRegex(BrokerError, "CINDRAL_DISPATCH_TOKEN is unset"):
                client.dispatch(REPO, ADAPTER_WORKFLOW, REF, {})
        opened.assert_not_called()

    def test_refusal_carries_the_broker_own_message(self) -> None:
        client = BrokerClient("http://127.0.0.1:8095", dispatch_token="tok")
        with mock.patch.object(brokerclient, "urlopen") as opened:
            opened.side_effect = self._http_error(403, "ref is not permitted for this workflow")
            with self.assertRaisesRegex(BrokerError, "ref is not permitted"):
                client.dispatch(REPO, ADAPTER_WORKFLOW, REF, {})
        self.assertEqual(opened.call_count, 1)

    def test_dispatch_refuses_without_a_token_rather_than_calling_out(self) -> None:
        client = BrokerClient("http://broker.invalid")
        with mock.patch.object(brokerclient, "urlopen") as opened:
            with self.assertRaisesRegex(BrokerError, "CINDRAL_DISPATCH_TOKEN is unset"):
                client.dispatch(REPO, ADAPTER_WORKFLOW, REF, {})
        opened.assert_not_called()

    def test_pool_token_is_not_sent_as_the_dispatch_token(self) -> None:
        # the broker refuses the two interchangeably, so a client that reused one
        # field for both would fail at the broker instead of here
        client = BrokerClient("http://127.0.0.1:8095", dispatch_token="dispatch", pool_token="pool")
        with mock.patch.object(brokerclient, "urlopen") as opened:
            opened.return_value = self._response({"devices": []})
            client.pool()
        self.assertEqual(opened.call_args[0][0].get_header("Authorization"), "Bearer pool")

    @staticmethod
    def _response(payload):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(payload).encode()
        response.__enter__ = lambda self: self
        response.__exit__ = lambda *args: False
        return response

    @staticmethod
    def _http_error(code, message):
        return HTTPError(
            "http://broker.invalid",
            code,
            message,
            {},
            BytesIO(json.dumps({"error": message}).encode()),
        )

    def test_an_unauthenticated_call_still_reaches_a_remote_host(self) -> None:
        # /healthz and /v1/route carry no credential, so the guard is about the
        # token and not the transport. A guard that fired on the URL alone would
        # refuse a request that has nothing to leak.
        for call in (lambda c: c.health(), lambda c: c.route({"repository": REPO})):
            with self.subTest(call=call):
                client = BrokerClient("http://broker.example.com")
                with mock.patch.object(brokerclient, "urlopen") as opened:
                    opened.side_effect = self._http_error(403, "refused")
                    with self.assertRaises(BrokerError):
                        call(client)
                self.assertEqual(opened.call_count, 1)
