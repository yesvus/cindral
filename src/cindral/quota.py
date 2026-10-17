"""Caching quota source: refresh schedule, staleness, and startup wiring.

The readings, and the endpoints they come from, live in billing.py. This holds
the cache, the refresh thread, and the environment wiring, and it keeps serving
the last good reading until that reading goes stale or its billing month ends.
"""
from collections.abc import Callable
import threading
import time

from .billing import (
    DEFAULT_MAX_AGE_SECONDS,
    DEFAULT_REFRESH_SECONDS,
    DEFAULT_STARTUP_TIMEOUT_SECONDS,
    MAX_AGE_BOUNDS,
    QUOTA_UNKNOWN,
    REASON_NEW_MONTH,
    REASON_NO_READING,
    REASON_STALE,
    REASON_UNREACHABLE,
    REFRESH_BOUNDS,
    SOURCE_NONE,
    STARTUP_TIMEOUT_BOUNDS,
    BillingClient,
    QuotaFailure,
    QuotaReading,
    _print_note,
    _utc_month,
    bounded_seconds,
)

# the read taken on the request thread after a month reset, which is the same
# situation as startup: the caller is waiting, and GitHub's webhook delivery
# window is shorter than the client's own timeout
POST_RESET_TIMEOUT_SECONDS = 3.0


class QuotaSource:
    """Serves the last good quota reading, refreshed on a bounded interval.

    A failed refresh keeps the previous reading, so a GitHub outage does not
    change routing, and a reading older than ``max_age`` is no longer served:
    the caller sees unknown, which follows the configured unknown-quota
    behavior. Failure notes print once per state change and name only a reason.
    """

    def __init__(
        self,
        client: BillingClient,
        organization: str,
        *,
        refresh_interval: float = DEFAULT_REFRESH_SECONDS,
        max_age: float = DEFAULT_MAX_AGE_SECONDS,
        clock: Callable[[], float] = time.time,
        note: Callable[[str], None] = _print_note,
    ) -> None:
        if not organization:
            raise ValueError("quota source requires an organization")
        if refresh_interval <= 0:
            raise ValueError("refresh interval must be positive")
        if max_age <= 0:
            raise ValueError("max age must be positive")
        self._client = client
        self._organization = organization
        self._refresh_interval = refresh_interval
        self._max_age = max_age
        self._clock = clock
        self._note = note
        self._cache: QuotaReading | None = None
        self._lock = threading.Lock()
        self._failing: str | None = None
        self._stop: threading.Event | None = None
        self._thread: threading.Thread | None = None

    def now(self) -> float:
        """The clock this source ages its readings by, for a caller that reports one."""
        return self._clock()

    def refresh(self, timeout: float | None = None) -> None:
        try:
            reading = self._client.read(self._organization, timeout)
        except QuotaFailure as exc:
            self._note_failure(exc.reason)
        except Exception:
            self._note_failure(REASON_UNREACHABLE)
        else:
            with self._lock:
                self._cache = reading
            if self._failing is not None:
                self._failing = None
                self._note(f"hosted quota read recovered from {reading.source}")

    def _note_failure(self, reason: str) -> None:
        if self._failing == reason:
            return
        self._failing = reason
        self._note(
            f"warning: hosted quota read failed: {reason},"
            " the last good reading serves until it goes stale"
        )

    def current(self) -> QuotaReading:
        now = self._clock()
        with self._lock:
            reading = self._cache
            failing = self._failing
        if reading is not None and now - reading.observed_at <= self._max_age:
            # the allowance resets on the first of the month, so a reading taken
            # in the previous one describes minutes that have come back
            if reading.period is None or reading.period == _utc_month(now):
                return reading
            # The reset is known the moment it happens, so the first caller
            # after it reads afresh rather than waiting out the interval with the
            # quota reported unknown. That read runs on the caller's thread, so
            # it is bounded like the startup read: a webhook delivery is given
            # seconds, not the client's full timeout, and a billing API that
            # hangs must not hold a route request open behind it.
            self.refresh(timeout=POST_RESET_TIMEOUT_SECONDS)
            with self._lock:
                refreshed = self._cache
            # a legacy reading reports no month, so age alone disqualifies it,
            # exactly as the check above does
            if refreshed is not None and (
                refreshed.period is None or refreshed.period == _utc_month(now)
            ):
                return refreshed
            return QuotaReading(
                status=QUOTA_UNKNOWN, source=SOURCE_NONE, observed_at=now, reason=REASON_NEW_MONTH
            )
        reason = failing or (REASON_STALE if reading is not None else REASON_NO_READING)
        return QuotaReading(status=QUOTA_UNKNOWN, source=SOURCE_NONE, observed_at=now, reason=reason)

    def start(self) -> None:
        # a handle left behind by a stop whose join timed out must not keep the
        # source from refreshing once that thread has finished
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="cindral-quota-refresh", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the refresh, keeping the handle when the join times out.

        A refresh in flight can outlast the join, and clearing the handle anyway
        let a later start spawn a second thread. The caller is shutting down, so
        a thread still running is reported rather than papered over.
        """
        if self._stop is not None:
            self._stop.set()
        thread = self._thread
        if thread is None:
            self._stop = None
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            self._note("warning: hosted quota refresh did not stop within the shutdown timeout")
            return
        self._stop = None
        self._thread = None

    def _loop(self) -> None:
        stop = self._stop
        assert stop is not None
        while not stop.wait(self._refresh_interval):
            self.refresh()


def configured_source(
    credential: "str | Callable[[], str] | None",
    getenv: Callable[[str], str | None],
    note: Callable[[str], None],
) -> QuotaSource | None:
    """Build the quota source the environment names, or None when unconfigured.

    The source reads once up front so the first private route sees a reading
    instead of unknown. Without an organization named, or a token that can read
    billing, there is no source and the unknown-quota behavior stands.
    """
    organization = (getenv("CINDRAL_BILLING_ORG") or "").strip()
    if not organization:
        return None
    if not credential:
        note("warning: CINDRAL_BILLING_ORG is set without a GitHub credential, hosted quota stays unknown")
        return None
    refresh = bounded_seconds(
        getenv("CINDRAL_BILLING_REFRESH_SECONDS") or "",
        DEFAULT_REFRESH_SECONDS, *REFRESH_BOUNDS, "CINDRAL_BILLING_REFRESH_SECONDS",
    )
    max_age = bounded_seconds(
        getenv("CINDRAL_BILLING_MAX_AGE_SECONDS") or "",
        DEFAULT_MAX_AGE_SECONDS, *MAX_AGE_BOUNDS, "CINDRAL_BILLING_MAX_AGE_SECONDS",
    )
    if max_age < refresh:
        # a staleness bound under the refresh interval would let every reading
        # expire before it is replaced, silently disabling the source
        raise ValueError("CINDRAL_BILLING_MAX_AGE_SECONDS must be at least CINDRAL_BILLING_REFRESH_SECONDS")
    # the first read runs inline, before the broker starts listening, so it is
    # bounded tighter than the periodic one: three sequential requests at the
    # refresh timeout would hold startup for half a minute during an outage
    startup_timeout = bounded_seconds(
        getenv("CINDRAL_BILLING_STARTUP_TIMEOUT_SECONDS") or "",
        DEFAULT_STARTUP_TIMEOUT_SECONDS, *STARTUP_TIMEOUT_BOUNDS,
        "CINDRAL_BILLING_STARTUP_TIMEOUT_SECONDS",
    )
    source = QuotaSource(
        BillingClient(credential),
        organization,
        refresh_interval=refresh,
        max_age=max_age,
        note=note,
    )
    source.refresh(timeout=startup_timeout)
    source.start()
    return source
