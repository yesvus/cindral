"""The two GitHub Actions billing endpoints, and what each can honestly report.

A reading states a status only when the endpoint it came from can establish it.
The legacy endpoint publishes an included-minute allowance and can say available
or exhausted. The enhanced usage report publishes no allowance, so it reports
what it observed and states no status: larger runners are always billed and never
eligible for included minutes, and public usage is listed the same way, so
billed minutes do not mean the allowance is gone.
"""
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

QUOTA_AVAILABLE = "available"
QUOTA_EXHAUSTED = "exhausted"
QUOTA_UNKNOWN = "unknown"

SOURCE_ACTIONS_BILLING = "actions_billing"
SOURCE_BILLING_USAGE = "billing_usage"
SOURCE_NONE = "none"

REASON_UNAUTHORIZED = "unauthorized"
REASON_UNREACHABLE = "unreachable"
REASON_MALFORMED = "malformed"
REASON_UNAVAILABLE = "unavailable"
REASON_STALE = "stale"
REASON_NEW_MONTH = "the billing period reset, so the cached reading no longer applies"
REASON_NO_READING = "no reading"

# when both billing endpoints are unreadable, report the reason that says what to fix first
_FAILURE_PRIORITY = (
    REASON_UNAUTHORIZED,
    REASON_UNREACHABLE,
    REASON_MALFORMED,
    REASON_UNAVAILABLE,
)

DEFAULT_REFRESH_SECONDS = 300.0
DEFAULT_MAX_AGE_SECONDS = 900.0
REFRESH_BOUNDS = (60.0, 3600.0)
MAX_AGE_BOUNDS = (60.0, 86400.0)
# the startup read blocks the broker from listening, so its per-request budget is
# tighter than the periodic refresh
DEFAULT_STARTUP_TIMEOUT_SECONDS = 3.0
STARTUP_TIMEOUT_BOUNDS = (1.0, 30.0)


def bounded_seconds(value: str, default: float, floor: float, ceiling: float, name: str) -> float:
    """Read a seconds setting held inside the range a refresh loop tolerates."""
    if not value.strip():
        return default
    try:
        seconds = float(value)
    except ValueError:
        raise ValueError(f"{name} must be a number of seconds: {value!r}") from None
    if not math.isfinite(seconds) or not floor <= seconds <= ceiling:
        raise ValueError(f"{name} must be between {floor:g} and {ceiling:g} seconds")
    return seconds


@dataclass(frozen=True)
class QuotaReading:
    """One hosted quota snapshot, carrying only what a routing decision needs.

    The billing payload is organization-private, so a reading exposes the
    aggregate minutes and its provenance and never a usage breakdown or the
    credential that read it.
    """

    status: str
    source: str
    observed_at: float
    included_minutes: float | None = None
    used_minutes: float | None = None
    paid_minutes: float | None = None
    remaining_minutes: float | None = None
    period: str | None = None
    reason: str = ""

    def age_seconds(self, now: float) -> float:
        return max(0.0, now - self.observed_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source": self.source,
            "observed_at": datetime.fromtimestamp(self.observed_at, tz=timezone.utc).isoformat(),
            "included_minutes": self.included_minutes,
            "used_minutes": self.used_minutes,
            "paid_minutes": self.paid_minutes,
            "remaining_minutes": self.remaining_minutes,
            "period": self.period,
            "reason": self.reason or None,
        }


class QuotaFailure(RuntimeError):
    """Raised when GitHub billing cannot be read, naming a reason only."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _utc_month(epoch: float) -> str:
    """The billing month an instant falls in, in UTC.

    GitHub resets included minutes on the first of the month UTC and the billing
    report is monthly, so a cached period is compared in that same zone.
    """
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m")


def _minutes(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _legacy_reading(payload: Mapping[str, Any], observed_at: float) -> QuotaReading:
    """The legacy org billing response names included, used, and paid minutes."""
    included = _minutes(payload.get("included_minutes"))
    used = _minutes(payload.get("total_minutes_used"))
    if included is None or used is None:
        raise QuotaFailure(REASON_MALFORMED)
    paid = _minutes(payload.get("total_paid_minutes_used")) or 0.0
    return QuotaReading(
        status=QUOTA_AVAILABLE if included > used else QUOTA_EXHAUSTED,
        source=SOURCE_ACTIONS_BILLING,
        observed_at=observed_at,
        included_minutes=included,
        used_minutes=used,
        paid_minutes=paid,
        remaining_minutes=max(included - used, 0.0),
    )


def _enhanced_reading(payload: Mapping[str, Any], period: str, observed_at: float) -> QuotaReading:
    """Read the enhanced usage report, which cannot settle the routing question.

    The enhanced billing platform publishes no included-minute allowance, so
    "was anything billed" is the only signal available and it does not mean the
    allowance is gone. Larger runners are always billed at the per-minute rate
    and are never eligible for included minutes, so an organization with its
    whole private allowance untouched that ran one larger-runner job reports
    billed minutes. Public-repository usage is listed identically to private
    usage, and no API reports included usage on this platform.

    Deriving exhaustion from that would move every private push onto a device
    for the rest of the billing month while hosted quota sat unused, and the
    reason on the commit status would read as a legitimate exhaustion. So this
    source reports its observations and states no status. The legacy endpoint is
    the only one that reports an allowance, and only it steers a lane.
    """
    items = payload.get("usageItems")
    if not isinstance(items, list):
        raise QuotaFailure(REASON_MALFORMED)
    used = 0.0
    paid = 0.0
    for item in items:
        if not isinstance(item, Mapping):
            raise QuotaFailure(REASON_MALFORMED)
        if str(item.get("product", "")).lower() != "actions":
            continue
        if str(item.get("unitType", "")).lower() != "minutes":
            continue
        quantity = _minutes(item.get("quantity"))
        if quantity is None:
            raise QuotaFailure(REASON_MALFORMED)
        used += quantity
        net = _minutes(item.get("netAmount")) or 0.0
        if net <= 0:
            continue
        price = _minutes(item.get("pricePerUnit")) or 0.0
        # billed dollars convert back to minutes at the sku's own rate
        paid += quantity if price <= 0 else net / price
    return QuotaReading(
        status=QUOTA_UNKNOWN,
        source=SOURCE_BILLING_USAGE,
        observed_at=observed_at,
        used_minutes=used,
        paid_minutes=paid,
        period=period,
    )


_REASON_FOR_STATUS = {
    401: REASON_UNAUTHORIZED,
    403: REASON_UNAUTHORIZED,
    404: REASON_UNAVAILABLE,
    410: REASON_UNAVAILABLE,
    400: REASON_MALFORMED,
    422: REASON_MALFORMED,
}


def _reason_for_status(status: int) -> str:
    return _REASON_FOR_STATUS.get(status, REASON_UNREACHABLE)


def _worst_failure(first: QuotaFailure, second: QuotaFailure) -> QuotaFailure:
    return first if _FAILURE_PRIORITY.index(first.reason) <= _FAILURE_PRIORITY.index(second.reason) else second


class BillingClient:
    """Reads Actions billing with the broker credential, legacy endpoint first.

    The legacy endpoint names included, used, and paid minutes but requires an
    organization owner; when it is gone or refuses the credential, the enhanced
    usage report for the current month is read instead, by organization id. A
    credential that can reach neither raises ``QuotaFailure`` so the caller
    reports unknown rather than zero.
    """

    def __init__(
        self,
        credential: "str | Callable[[], str]",
        api_url: str = "https://api.github.com",
        timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credential = credential
        self._api_url = api_url.rstrip("/")
        self._timeout = timeout
        self._clock = clock
        self._organization_ids: dict[str, int] = {}

    @property
    def _token(self) -> str:
        """The bearer for one request, read fresh so an expiring token works."""
        return self._credential() if callable(self._credential) else self._credential

    def read(self, organization: str, timeout: float | None = None) -> QuotaReading:
        failure: QuotaFailure | None = None
        try:
            return self._actions_billing(organization, timeout)
        except QuotaFailure as first:
            failure = first
        try:
            organization_id = self._organization_id(organization, timeout)
            year, month = self._period()
            url = f"{self._api_url}/organizations/{organization_id}/settings/billing/usage?year={year}&month={month}"
            payload = self._get_json(url, timeout)
        except QuotaFailure as second:
            raise _worst_failure(failure, second) from second
        return _enhanced_reading(payload, f"{year:04d}-{month:02d}", self._clock())

    def _actions_billing(self, organization: str, timeout: float | None) -> QuotaReading:
        url = f"{self._api_url}/orgs/{quote(organization, safe='')}/settings/billing/actions"
        return _legacy_reading(self._get_json(url, timeout), self._clock())

    def _organization_id(self, organization: str, timeout: float | None) -> int:
        cached = self._organization_ids.get(organization)
        if cached is not None:
            return cached
        url = f"{self._api_url}/orgs/{quote(organization, safe='')}"
        identifier = self._get_json(url, timeout).get("id")
        if isinstance(identifier, bool) or not isinstance(identifier, int):
            raise QuotaFailure(REASON_MALFORMED)
        self._organization_ids[organization] = identifier
        return identifier

    def _period(self) -> tuple[int, int]:
        now = datetime.fromtimestamp(self._clock(), tz=timezone.utc)
        return now.year, now.month

    def _get_json(self, url: str, timeout: float | None = None) -> dict[str, Any]:
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout if timeout is None else timeout) as response:
                body = response.read()
        except HTTPError as exc:
            raise QuotaFailure(_reason_for_status(exc.code)) from exc
        except OSError as exc:
            raise QuotaFailure(REASON_UNREACHABLE) from exc
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise QuotaFailure(REASON_MALFORMED) from exc
        if not isinstance(payload, dict):
            raise QuotaFailure(REASON_MALFORMED)
        return payload


def _print_note(message: str) -> None:
    print(message, flush=True)
