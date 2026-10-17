"""The delivery ledger's own transitions, apart from dispatching one.

Kept out of the dispatch path because these are the writes: claiming a delivery,
finishing it, and saying a settled delivery is handled rather than handing a
refusal to the device pool for a dispatch nobody made. The ledger is durable
because these are the statements that make it so.
"""
import sqlite3

from .deliveries import ACCEPTED, REJECTED, UNCERTAIN, Delivery, DeliveryStore


def state_to_status(state: str) -> int:
    if state in {ACCEPTED, "duplicate"}:
        return 202
    if state == REJECTED:
        return 422
    return 503


def _finish(store: DeliveryStore, record: Delivery, state: str, error: str = "") -> tuple[str, str]:
    try:
        store.finish(record.repository, record.delivery, state, error)
    except (sqlite3.Error, RuntimeError):
        return UNCERTAIN, "dispatch result could not be recorded"
    return state, error


