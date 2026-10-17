"""Tests for the two billing endpoints and what each may report.

The routing consequence of these readings is covered in test_quota.py.
"""
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from cindral.billing import (
    QUOTA_AVAILABLE,
    QUOTA_EXHAUSTED,
    QUOTA_UNKNOWN,
    REASON_UNAUTHORIZED,
    REASON_UNREACHABLE,
    SOURCE_ACTIONS_BILLING,
    SOURCE_BILLING_USAGE,
    BillingClient,
    QuotaFailure,
    QuotaReading,
    bounded_seconds,
)

SEPTEMBER = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc).timestamp()
TOKEN = "secret-token-value"
LEGACY_PAYLOAD = {
    "total_minutes_used": 305,
    "total_paid_minutes_used": 0,
    "included_minutes": 3000,
    "minutes_used_breakdown": {"UBUNTU": 305, "MACOS": 0, "WINDOWS": 0},
}
USAGE_ITEMS = [
    {
        "date": "2026-09-01",
        "product": "Actions",
        "sku": "Actions Linux",
        "quantity": 100,
        "unitType": "minutes",
        "pricePerUnit": 0.008,
        "grossAmount": 0.8,
        "discountAmount": 0,
        "netAmount": 0.8,
        "organizationName": "example-org",
        "repositoryName": "example-org/example-app",
    },
    {
        "date": "2026-09-02",
        "product": "Actions",
        "sku": "Actions Windows",
        "quantity": 50,
        "unitType": "minutes",
        "pricePerUnit": 0.016,
        "grossAmount": 0.8,
        "discountAmount": 0.8,
        "netAmount": 0,
        "organizationName": "example-org",
        "repositoryName": "example-org/example-app",
    },
]


class Response:
    def __init__(self, body: bytes = b""):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class BillingClientTest(unittest.TestCase):
    def fetch(self, responses):
        client = BillingClient(TOKEN, clock=Clock(SEPTEMBER))
        with patch("cindral.billing.urlopen", side_effect=responses) as urlopen:
            outcome: tuple[QuotaReading | None, Exception | None]
            try:
                outcome = (client.read("example-org"), None)
            except QuotaFailure as exc:
                outcome = (None, exc)
        return outcome, urlopen.call_args_list

    def test_legacy_billing_reports_included_used_and_remaining(self) -> None:
        (reading, error), calls = self.fetch([Response(json.dumps(LEGACY_PAYLOAD).encode())])
        self.assertIsNone(error)
        self.assertEqual(reading.status, QUOTA_AVAILABLE)
        self.assertEqual(reading.included_minutes, 3000.0)
        self.assertEqual(reading.used_minutes, 305.0)
        self.assertEqual(reading.paid_minutes, 0.0)
        self.assertEqual(reading.remaining_minutes, 2695.0)
        self.assertEqual(reading.source, SOURCE_ACTIONS_BILLING)
        self.assertIsNone(reading.period)
        self.assertEqual(len(calls), 1)
        request = calls[0].args[0]
        self.assertEqual(request.full_url, "https://api.github.com/orgs/example-org/settings/billing/actions")
        self.assertIn(f"Bearer {TOKEN}", request.headers["Authorization"])

    def test_spent_included_minutes_read_as_exhausted_never_negative(self) -> None:
        payload = {"total_minutes_used": 3200, "total_paid_minutes_used": 200, "included_minutes": 3000}
        (reading, error), _ = self.fetch([Response(json.dumps(payload).encode())])
        self.assertIsNone(error)
        self.assertEqual(reading.status, QUOTA_EXHAUSTED)
        self.assertEqual(reading.remaining_minutes, 0.0)
        self.assertEqual(reading.paid_minutes, 200.0)

    def test_missing_legacy_numbers_read_as_malformed_never_zero(self) -> None:
        responses = [
            Response(b'{"total_minutes_used": 10}'),
            Response(b'{"id": 42}'),
            Response(b'{"usageItems": "not-a-list"}'),
        ]
        (reading, error), _ = self.fetch(responses)
        self.assertIsNone(reading)
        self.assertEqual(error.reason, "malformed")

    def test_moved_legacy_endpoint_falls_back_to_enhanced_usage(self) -> None:
        moved = HTTPError("url", 410, "gone", {}, None)
        responses = [moved, Response(b'{"id": 42}'), Response(json.dumps({"usageItems": USAGE_ITEMS}).encode())]
        (reading, error), calls = self.fetch(responses)
        self.assertIsNone(error)
        self.assertEqual(reading.source, SOURCE_BILLING_USAGE)
        self.assertEqual(reading.used_minutes, 150.0)
        # the linux item was billed at its own rate, the windows item was discounted
        self.assertEqual(reading.paid_minutes, 100.0)
        self.assertIsNone(reading.included_minutes)
        self.assertIsNone(reading.remaining_minutes)
        self.assertEqual(reading.period, "2026-09")
        self.assertEqual(calls[1].args[0].full_url, "https://api.github.com/orgs/example-org")
        self.assertEqual(
            calls[2].args[0].full_url,
            "https://api.github.com/organizations/42/settings/billing/usage?year=2026&month=9",
        )

    def test_discounted_usage_reports_observations_without_a_verdict(self) -> None:
        # every minute discounted is consistent with a healthy allowance and also
        # with public-only usage, which the platform lists the same way
        items = [dict(item, netAmount=0, discountAmount=item["grossAmount"]) for item in USAGE_ITEMS]
        moved = HTTPError("url", 410, "gone", {}, None)
        responses = [moved, Response(b'{"id": 42}'), Response(json.dumps({"usageItems": items}).encode())]
        (reading, error), _ = self.fetch(responses)
        self.assertIsNone(error)
        self.assertEqual(reading.status, QUOTA_UNKNOWN)
        self.assertEqual(reading.used_minutes, 150.0)
        self.assertEqual(reading.paid_minutes, 0.0)

    def test_billed_larger_runner_minutes_never_read_as_exhaustion(self) -> None:
        # Larger runners are always billed and never eligible for included
        # minutes, so an org with its whole allowance untouched that ran one
        # larger-runner job reports billed minutes. Calling that exhaustion moved
        # every private push to a device for the rest of the month, while hosted
        # quota sat unused and the status read as a legitimate exhaustion.
        always_billed = {
            "usageItems": [
                {
                    "date": "2026-09-01",
                    "product": "actions",
                    "sku": "Larger Runner Linux",
                    "quantity": 10,
                    "unitType": "minutes",
                    "pricePerUnit": 0.08,
                    "amount": 0.8,
                    "netAmount": 0.8,
                }
            ]
        }
        moved = HTTPError("url", 410, "gone", {}, None)
        responses = [moved, Response(b'{"id": 42}'), Response(json.dumps(always_billed).encode())]
        (reading, error), _ = self.fetch(responses)
        self.assertIsNone(error)
        self.assertEqual(reading.status, QUOTA_UNKNOWN)
        self.assertEqual(reading.paid_minutes, 10.0)
        self.assertIsNone(reading.remaining_minutes)
        self.assertIsNone(reading.included_minutes)

    def test_an_empty_usage_report_claims_neither_verdict(self) -> None:
        # an empty list is also what a filter that silently matched nothing
        # returns, so it is not positive evidence of a fresh allowance
        moved = HTTPError("url", 410, "gone", {}, None)
        responses = [moved, Response(b'{"id": 42}'), Response(b'{"usageItems": []}')]
        (reading, error), _ = self.fetch(responses)
        self.assertIsNone(error)
        self.assertEqual(reading.status, QUOTA_UNKNOWN)
        self.assertEqual(reading.used_minutes, 0.0)

    def test_usage_report_ignores_other_products_and_units(self) -> None:
        items = USAGE_ITEMS + [
            {"product": "Codespaces", "sku": "basic", "quantity": 500, "unitType": "hours"},
            {"product": "Actions", "sku": "Actions Storage", "quantity": 7, "unitType": "GB-hours"},
        ]
        moved = HTTPError("url", 410, "gone", {}, None)
        responses = [moved, Response(b'{"id": 42}'), Response(json.dumps({"usageItems": items}).encode())]
        (reading, error), _ = self.fetch(responses)
        self.assertIsNone(error)
        self.assertEqual(reading.used_minutes, 150.0)

    def test_unauthorized_on_both_endpoints_raises_without_numbers(self) -> None:
        responses = [
            HTTPError("url", 403, "forbidden", {}, None),
            Response(b'{"id": 42}'),
            HTTPError("url", 403, "forbidden", {}, None),
        ]
        (reading, error), _ = self.fetch(responses)
        self.assertIsNone(reading)
        self.assertEqual(error.reason, "unauthorized")

    def test_network_failure_on_both_endpoints_reports_unreachable(self) -> None:
        responses = [URLError("connection refused"), URLError("connection refused")]
        (reading, error), _ = self.fetch(responses)
        self.assertIsNone(reading)
        self.assertEqual(error.reason, "unreachable")

    def test_moved_legacy_with_malformed_usage_reports_malformed(self) -> None:
        responses = [
            HTTPError("url", 410, "gone", {}, None),
            Response(b'{"id": 42}'),
            Response(b'{"usageItems": "not-a-list"}'),
        ]
        (reading, error), _ = self.fetch(responses)
        self.assertIsNone(reading)
        self.assertEqual(error.reason, "malformed")

    def test_organization_id_is_resolved_once_and_reused(self) -> None:
        moved = HTTPError("url", 410, "gone", {}, None)
        usage = Response(json.dumps({"usageItems": []}).encode())
        client = BillingClient(TOKEN, clock=Clock(SEPTEMBER))
        with patch("cindral.billing.urlopen", side_effect=[moved, Response(b'{"id": 42}'), usage, moved, usage]):
            client.read("example-org")
            client.read("example-org")
        self.assertEqual(client._organization_ids, {"example-org": 42})

    def test_failures_and_readings_never_carry_the_credential(self) -> None:
        responses = [
            HTTPError("url", 403, "forbidden", {}, None),
            Response(b'{"id": 42}'),
            HTTPError("url", 403, "forbidden", {}, None),
        ]
        (reading, error), _ = self.fetch(responses)
        self.assertNotIn(TOKEN, str(error))
        self.assertNotIn(TOKEN, repr(error))
        good = QuotaReading(
            status=QUOTA_AVAILABLE,
            source=SOURCE_ACTIONS_BILLING,
            observed_at=SEPTEMBER,
            included_minutes=3000.0,
            used_minutes=305.0,
            remaining_minutes=2695.0,
        )
        self.assertNotIn(TOKEN, json.dumps(good.as_dict()))

