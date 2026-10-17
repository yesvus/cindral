"""One delivery, dispatched once, with the result recorded whatever happens.

The durable part of the push path in one function: claim the row so a second
drainer finds nothing to send, send it, and record the outcome in the same
transaction as the release that owes it a verdict. The states are named rather
than spread, because "a refusal that may be covered" and "a run that may already
exist" are the two answers that must never be confused.
"""
from .deliveries import ACCEPTED, REJECTED, UNCERTAIN, Delivery, DeliveryStore
from .delivery_state import _finish
from .delivery_spend import _attribute, _release
from .github import GitHubClient, GitHubDispatchError
from .metering import MeteredLedger

TERMINAL_STATES = (ACCEPTED, REJECTED, UNCERTAIN)



def dispatch_record(
    store: DeliveryStore,
    github: GitHubClient,
    record: Delivery,
    ledger: MeteredLedger | None = None,
    reservation: int | None = None,
) -> tuple[str, str]:
    if not store.claim(record.repository, record.delivery):
        # nothing was dispatched, so any hold taken for this one goes back. It is
        # taken before the claim on purpose, so a redelivery of a delivery that is
        # already accepted costs nothing rather than spending the budget of a
        # dispatch nobody will make.
        _release(ledger, reservation)
        current = store.get(record.repository, record.delivery)
        if current is None:
            return UNCERTAIN, "delivery record vanished before dispatch"
        if current.state in TERMINAL_STATES:
            # A terminal state belongs to the attempt that won the claim. A
            # caller that read it as its own result would hand a refused
            # delivery to the device pool for a dispatch it never made, so a
            # settled delivery reads as handled rather than as an outcome.
            return "duplicate", ""
        return current.state, ""
    try:
        github.dispatch(record.repository, record.workflow, record.ref, record.inputs)
    except GitHubDispatchError as exc:
        # An unset status code is a URLError: DNS failure, refused connection and
        # TLS failure all happen before a byte reaches GitHub, so no run exists
        # and the other lane is free to take the push. A 5xx is the one answer
        # whose run may be real. A 403 or 429 is GitHub refusing for now rather
        # than refusing outright, so it stays retryable and is not terminal.
        code = exc.status_code
        state = UNCERTAIN if code is not None and (code >= 500 or code in {403, 429}) else REJECTED
        if state == REJECTED:
            # an answer that says the run did not start, so nothing was charged.
            # An UNCERTAIN one may have started the run, and releasing that would
            # book a run nobody pays for.
            _release(ledger, reservation)
        return _finish(store, record, state, str(exc))
    except Exception as exc:
        state, _ = _finish(store, record, UNCERTAIN, str(exc))
        if state == REJECTED:
            # an answer from GitHub that the run did not start, so the hold goes
            # back. An UNCERTAIN one may have started the run, and releasing that
            # would book a run nobody pays for.
            _release(ledger, reservation)
        return state, "GitHub dispatch result is unknown"
    # the run exists, so the spend does. The hold taken at decision time is
    # already counted, so all that is left is to say what it was for.
    _attribute(ledger, reservation, record)
    return _finish(store, record, ACCEPTED)


