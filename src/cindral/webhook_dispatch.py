"""Dispatch signed push deliveries through the durable broker inbox."""
import hashlib
import sqlite3
import threading
from collections.abc import Callable
from typing import Any

from .budget import release_reservation
from .deliveries import ACCEPTED, READY, REJECTED, UNCERTAIN, Delivery, DeliveryStore
from .github import GitHubAPIError, GitHubClient, GitHubDispatchError
from .metering import MeteredLedger
from .delivery_spend import _attribute, _release, delivery_fallback, reserve_for
from .delivery_dispatch import dispatch_record
from .delivery_state import state_to_status
from .refusal import hand_off, missing_workflow, should_fallback
from .models import RouteRequest
from .policy import DispatchNotPermitted, Policy, RouteUnavailable
from .webhook import DELIVERY_HEADER


def recover_ready(
    store: DeliveryStore,
    github: GitHubClient | None,
    policy: Policy,
    *,
    fallback: Callable[[Delivery], None] | None = None,
    ledger: MeteredLedger | None = None,
) -> None:
    """Replay every delivery the queue still holds.

    There is no check run to close here. A replayed delivery is a
    `workflow_dispatch` the broker never queued, so there is no job of ours to
    attach a check to, and a run the device takes over publishes its own.

    `fallback` and `ledger` are keyword-only on purpose. A fourth positional
    argument that used to be the ledger became the device hand-off when it was
    added, and a caller passing it positionally went on asserting against a
    ledger the function was never given.
    """
    if github is not None:
        for record in store.pending():
            hold: int | None = None
            try:
                try:
                    policy.check_dispatch(record.repository, record.workflow, record.ref)
                except DispatchNotPermitted as exc:
                    # the device is offered before refusing, matching the branch
                    # below. A repository the policy still serves on a device lost
                    # its dispatch authorisation, not its device authorisation, so
                    # dropping the run here is what made a device-only repository
                    # silently stop building. hand_off re-checks permits_device, so
                    # a dispatch-only repository is unaffected.
                    if store.reject_ready(record.repository, record.delivery, str(exc)):
                        print(f"warning: delivery {record.delivery} for {record.repository} is no longer authorized", flush=True)
                        hand_off(record, str(exc), policy, fallback)
                    continue
                if not github.workflow_exists(record.repository, record.workflow):
                    # the adapter is gone, so GitHub cannot serve this
                    # repository either, and the device is the lane that is left
                    reason = f"{missing_workflow(record.repository, record.workflow)}; it was removed before the replay"
                    if store.reject_ready(record.repository, record.delivery, reason):
                        print(f"warning: delivery {record.delivery} for {record.repository}: {reason}", flush=True)
                        hand_off(record, reason, policy, fallback)
                    continue
                # a recovery replay is a real dispatch, so it holds the metered
                # budget like any other
                hold, refusal = reserve_for(policy, record.inputs.get("cindral_lane"))
                if refusal is not None:
                    # left ready, so the next pass tries again once there is room
                    print(
                        f"warning: delivery {record.delivery} for {record.repository}"
                        f" is waiting on the metered budget: {refusal}",
                        flush=True,
                    )
                    continue
                state, error = dispatch_record(store, github, record, ledger, hold)
                if state == ACCEPTED:
                    continue
                print(f"warning: delivery {record.delivery} for {record.repository}: {state}: {error}", flush=True)
                if should_fallback(state):
                    hand_off(record, error or "GitHub refused the dispatch", policy, fallback)
            except Exception as exc:
                # everything dispatch_record raises comes from the delivery store
                # before it sends anything, so this is a dispatch that did not
                # happen and the hold goes back with it
                _release(ledger, hold)
                print(f"warning: delivery {record.delivery} for {record.repository} could not be replayed: {exc}", flush=True)


def replay_loop(
    store: DeliveryStore,
    github: GitHubClient | None,
    policy: Policy,
    stop: threading.Event,
    interval: float = 60,
    *,
    fallback: Callable[[Delivery], None] | None = None,
    ledger: MeteredLedger | None = None,
) -> None:
    while not stop.is_set():
        try:
            recover_ready(store, github, policy, fallback=fallback, ledger=ledger)
        except Exception as exc:
            print(f"warning: webhook delivery replay paused: {exc}", flush=True)
        stop.wait(interval)


class DispatchSendingMixin:
    """The one place a dispatch leaves for GitHub, so one place records it.

    Three paths used to call GitHub themselves: the push webhook, the dispatch
    endpoint and a pull request. A record added to one of them counted only that
    one's spending, and a ceiling counted from a source with a hole in it reads
    as enforced while two thirds of the spend is not. So sending and counting
    cannot be separated here.
    """

    server: Any

    def _name_hold(self, decision, repository: str, workflow: str, ref: str) -> None:
        """Point a metered hold at the dispatch it turned out to be for.

        The hold is taken before routing knows the repository or the workflow, so
        without this the month cannot be read by whoever reconciles it against a
        bill. A ledger that cannot be written is logged, never fatal: the run has
        already started.
        """
        ledger = getattr(self.server, "metered", None)
        if ledger is None or decision.reservation is None:
            return
        try:
            ledger.attribute(decision.reservation, repository, workflow, ref)
        except Exception as exc:  # noqa: BLE001 - see above
            self.log_error("could not attribute a metered dispatch: %s", exc)

    def _send_dispatch(
        self, repository: str, workflow: str, ref: str, inputs: dict[str, str]
    ) -> None:
        self.server.github.dispatch(repository, workflow, ref, inputs)


class WebhookDispatchMixin:
    server: Any

    def _adapter_absent(
        self, event: Any, workflow: str, reason: str, fallback: Callable[[], None] | None
    ) -> None:
        """Answer a push whose repository has no adapter to dispatch.

        A repository the policy serves on a device is queued there, because
        what is missing is the adapter and not the pool, and a device run is
        what this push would have had before hosted-first routing. One with no
        device has nowhere else to put it, so the refusal is written on the
        commit: a 202 is a success code, and an `ignored` push that leaves no
        check behind it is indistinguishable from one that has not run yet.
        """
        # The policy is asked here rather than trusting the caller to have built
        # a fallback only for a repository it serves on a device. A repository
        # served on GitHub alone has no device to take this, and a caller that
        # stops consulting push_lane would otherwise queue a commit on local
        # hardware that the device path would have refused.
        eligible = self.server.policy.permits_device(event.repository)
        if eligible and fallback is not None:
            fallback()
            return
        # A 202 with nothing behind it reads as a push that has not run yet, so
        # the commit is told what is missing, whether a device could have taken
        # it, and that this ran nowhere. Those are three different things and
        # they have three different fixes: an adapter, a policy entry, and a
        # lane. A message naming only the first sends an operator to the wrong
        # one.
        why = "" if eligible else "; the policy does not serve this repository on a device"
        self._post_refusal(event, f"{reason}{why}; this push ran nowhere")
        self._send(202, {"status": "ignored", "reason": f"{reason}{why}", "workflow": workflow})

    def _post_refusal(self, event: Any, reason: str) -> bool:
        """Write a refusal on the pushed commit, so a silent push is not silent.

        The `cindral/ci` context is where a maintainer looks for what a push
        did, and a delivery that ran nowhere leaves that context absent, which
        reads the same as a run that has not started. The reason is the one
        naming the missing file, so the commit states the thing to fix.
        """
        if self.server.github is None or not event.after:
            return False
        try:
            self.server.github.post_status(
                event.repository, event.after, "error", reason,
                context=self.server.status_context,
            )
            return True
        except GitHubAPIError:
            return False

    def _dispatch_push(
        self, event: Any, branch: str, body: bytes, fallback: Callable[[], None] | None = None
    ) -> None:
        """Hand a push to GitHub's runners, or to `fallback` when it will not go.

        `fallback` answers the request itself, so a caller that passes the local
        enqueue gets a queued job and a 202 rather than a dispatch error. It is
        the whole of "prefer hosted, use the devices when hosted is not
        available", and it fires only where GitHub refused the request, never
        where the answer was lost.
        """
        store = self.server.deliveries
        if store is None:
            self._send(503, {"error": "signed dispatch requires CINDRAL_JOBS_DB"})
            return
        try:
            self.server.policy.check_dispatch(event.repository, self.server.workflow_file, branch)
        except DispatchNotPermitted as exc:
            self._send(403, {"error": str(exc)})
            return
        fingerprint = hashlib.sha256(body).hexdigest()
        # without the header the payload hash is the only identity, which is why
        # the fingerprint check below cannot fire on this path
        delivery = self.headers.get(DELIVERY_HEADER) or fingerprint
        try:
            existing = store.get(event.repository, delivery)
        except sqlite3.Error:
            self._send(503, {"error": "delivery state could not be read"})
            return
        if existing is not None:
            if existing.fingerprint != fingerprint:
                self._send(409, {"error": "delivery ID belongs to a different payload"})
                return
            if existing.state == READY and self.server.github is not None:
                try:
                    if not self.server.github.workflow_exists(event.repository, existing.workflow):
                        # the adapter is gone, so GitHub cannot serve this push
                        # either, which is the same missing-adapter answer the
                        # first attempt would have found
                        reason = f"{missing_workflow(event.repository, existing.workflow)}; it was removed before the replay"
                        if store.reject_ready(event.repository, delivery, reason):
                            self._adapter_absent(event, existing.workflow, reason, fallback)
                            return
                except GitHubAPIError as exc:
                    self._send(502, {"error": str(exc)})
                    return
                except sqlite3.Error:
                    self._send(503, {"error": "delivery state could not be recorded"})
                    return
                hold, refusal = reserve_for(
                    self.server.policy, existing.inputs.get("cindral_lane")
                )
                if refusal is not None:
                    self._send(409, {"error": refusal, "delivery": delivery})
                    return
                try:
                    state, error = dispatch_record(
                        store, self.server.github, existing,
                        getattr(self.server, "metered", None), hold,
                    )
                except sqlite3.Error:
                    release_reservation(self.server, hold)
                    self._send(503, {"error": "delivery state could not be read"})
                    return
                # The replay follows the same rule as the live dispatch above,
                # or one commit would be queued on a device when it arrived and
                # dropped when it was retried, which is the one distinction a
                # commit status cannot explain afterwards. permits_device is
                # asked here rather than trusted from the caller, which builds
                # this fallback from push_lane: a repository with no device lane
                # must not reach local hardware even if that ever changes.
                device_lane = fallback is not None and self.server.policy.permits_device(event.repository)
                if device_lane and should_fallback(state):
                    print(f"warning: delivery {delivery} for {event.repository} is {state}: {error}", flush=True)
                    fallback()
                    return
                self._send(state_to_status(state),
                           {"status": state, "delivery": delivery, "error": error})
                return
            if existing.state != READY:
                self._send(state_to_status(existing.state),
                           {"status": "duplicate" if existing.state == ACCEPTED else existing.state, "delivery": delivery})
                return
        if self.server.github is None:
            self._send(503, {"error": "GitHub dispatch token is not configured"})
            return
        try:
            if not self.server.github.workflow_exists(event.repository, self.server.workflow_file):
                self._adapter_absent(
                    event, self.server.workflow_file,
                    missing_workflow(event.repository, self.server.workflow_file), fallback,
                )
                return
            # The lane the policy names, not "auto": with unknown quota "auto"
            # resolves to the local pool, so asking for it would send the push to
            # a self-hosted runner whatever push_lane says. Only a push_lane of
            # "auto" asks for it, and a repository with no device at all gets
            # hosted because it is the only lane left.
            request = RouteRequest(
                requested_lane=(
                    "auto" if self.server.policy.push_lane(event.repository) == "auto" else "hosted"
                ),
                repository_visibility="private" if event.private else "public",
                quota_status="unknown",
            )
            decision, reservation = self._dispatch_decision(request, event.repository, allow_busy=True)
        except RouteUnavailable as exc:
            self._send(409, {"error": str(exc)})
            return
        except GitHubAPIError as exc:
            self._send(502, {"error": str(exc)})
            return
        inputs = {
            "cindral_lane": decision.lane,
            "cindral_target": request.target or "",
            "cindral_reason": decision.reason,
        }
        try:
            record = store.register(
                event.repository, delivery, fingerprint, self.server.workflow_file, branch, inputs,
                event.after,
            )
        except sqlite3.Error:
            self._release_reservation(reservation)
            release_reservation(self.server, decision.reservation)
            self._send(503, {"error": "delivery could not be recorded"})
            return
        if record.fingerprint != fingerprint:
            self._release_reservation(reservation)
            release_reservation(self.server, decision.reservation)
            self._send(409, {"error": "delivery ID belongs to a different payload"})
            return
        try:
            state, error = dispatch_record(
                store, self.server.github, record,
                getattr(self.server, "metered", None), decision.reservation,
            )
        except sqlite3.Error:
            # The metered hold goes back, because this can only be the claim.
            # Everything dispatch_record does after sending either swallows its
            # own errors (naming the hold) or returns UNCERTAIN, so a store error
            # escaping it means nothing was sent and the month must not be charged
            # for a dispatch that never happened.
            self._release_reservation(reservation)
            release_reservation(self.server, decision.reservation)
            self._send(503, {"error": "delivery state could not be read"})
            return
        if state == "duplicate":
            self._release_reservation(reservation)
            release_reservation(self.server, decision.reservation)
            self._send(202, {"status": "duplicate", "delivery": delivery})
            return
        if state != ACCEPTED:
            self._release_reservation(reservation)
            # Only REJECTED is safe to fall back from. Every UNCERTAIN here is
            # a 5xx, where a run may already exist, so running the same commit
            # on a device as well would report it twice and book a runner
            # twice. A REJECTED means nothing was dispatched, so the other lane
            # still has this push. should_fallback holds the same rule for the
            # two replay paths, so all three agree on which refusals are safe.
            if fallback is not None and should_fallback(state):
                fallback()
                return
            if state in {UNCERTAIN, REJECTED}:
                # BaseHTTPRequestHandler.log_error routes to the overridden
                # log_message, which returns, so this has to print to be seen
                print(f"warning: delivery {delivery} for {event.repository} is {state}: {error}", flush=True)
            self._send(state_to_status(state),
                       {"status": state, "error": error, "delivery": delivery})
            return
        if record.inputs != inputs:
            self._release_reservation(reservation)
            self._send(202, {"status": "accepted", "delivery": delivery})
            return
        self._send(200, {
            **decision.as_dict(), "dispatched": True, "repository": event.repository,
            "branch": branch, "delivery": self.headers.get(DELIVERY_HEADER, ""),
        })
