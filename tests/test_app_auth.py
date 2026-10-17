"""Tests for App installation auth."""
import base64
import json
import threading
import pathlib
import unittest

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding

from cindral.app_auth import (
    JWT_TTL_SECONDS,
    TOKEN_REFRESH_MARGIN_SECONDS,
    AppAuth,
    _expiry,
    AppAuthError,
    mint_jwt,
)

CLIENT_ID = "Iv23liGmKNTWwkv5bLO2"
# GitHub installation tokens live one hour
TOKEN_LIFETIME_SECONDS = 3600


def generate_ed25519_pem() -> bytes:
    return ed25519.Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def generate_key() -> bytes:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


class FakeTransport:
    """Records every call so a test can assert on what left the process."""

    def __init__(self, clock=1000.0, installations=None, repositories=None):
        # the credential is recorded, not the path, because a path cannot hold a
        # key and asserting on one proves nothing about containment
        self.calls: list[tuple[str, str, dict | None]] = []
        self.now = clock
        self.tokens_issued = 0
        self.installations = installations
        self.repositories = repositories

    def __call__(self, path, token, body=None):
        self.calls.append((path, token, body))
        if path.startswith("/app/installations?"):
            query = dict(pair.split("=") for pair in path.split("?", 1)[1].split("&"))
            page, per_page = int(query.get("page", 1)), int(query.get("per_page", 100))
            listed = self.installations
            if listed is None:
                listed = [
                {
                    "id": 165836227,
                    "account": {"login": "example-org"},
                    "repository_selection": "all",
                    "suspended_at": None,
                }
                ]
            start = (page - 1) * per_page
            return listed[start : start + per_page]
        if path.endswith("/access_tokens"):
            self.tokens_issued += 1
            # relative to the fake clock, so the refresh margin is actually exercised
            from datetime import datetime, timedelta, timezone

            expires = datetime.fromtimestamp(self.now + 3600, tz=timezone.utc)
            return {
                "token": f"ghs_token_{self.tokens_issued}",
                "expires_at": expires.isoformat().replace("+00:00", "Z"),
            }
        if path.startswith("/installation/repositories"):
            listed = self.repositories
            if listed is None:
                listed = [{"full_name": "example-org/cindral"}]
            return {"repositories": listed}
        raise AssertionError(f"unexpected path {path}")

    def authorization(self, index: int) -> str:
        return self.calls[index][1]


class MintJwtTest(unittest.TestCase):
    def test_a_jwt_claims_the_client_id_and_stays_inside_the_ceiling(self) -> None:
        token = mint_jwt(CLIENT_ID, generate_key(), now=1000.0)
        header, claims, _ = token.split(".")
        import base64

        def part(value: str) -> dict:
            return json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))

        self.assertEqual(part(header)["alg"], "RS256")
        self.assertEqual(part(claims)["iss"], CLIENT_ID)
        # backdated so a small clock difference does not reject it
        self.assertEqual(part(claims)["iat"], 940)
        self.assertLessEqual(part(claims)["exp"] - part(claims)["iat"], JWT_TTL_SECONDS + 60)

    def test_an_unreadable_key_does_not_quote_the_key(self) -> None:
        # the loader's own message is not evidence, so the key is built to make
        # it quotable and the error is checked against the whole input
        key = serialization.load_pem_private_key(generate_key(), password=None)
        der = key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        for material in (
            b"-----BEGIN RSA PRIVATE KEY-----\nnot a key\n-----END RSA PRIVATE KEY-----\n",
            b"-----BEGIN PRIVATE KEY-----\n" + base64.b64encode(der[:40]) + b"\n-----END PRIVATE KEY-----\n",
            der[:40],
        ):
            with self.subTest(material=len(material)):
                with self.assertRaises(AppAuthError) as caught:
                    mint_jwt(CLIENT_ID, material)
                message = str(caught.exception)
                self.assertNotIn("BEGIN", message)
                self.assertNotIn("PRIVATE KEY", message)
                self.assertNotIn(base64.b64encode(der).decode()[:32], message)
                self.assertLess(len(message), 200)

    def test_the_signature_verifies_against_the_public_key(self) -> None:
        # nothing verifies this at runtime, because a client only signs, so a
        # corrupted signature, a SHA-1 digest, or padded base64url would ship
        # green and be rejected by GitHub
        key = generate_key()
        token = mint_jwt(CLIENT_ID, key, now=1000.0)
        header, claims, signature = token.split(".")
        public = serialization.load_pem_private_key(key, password=None).public_key()
        public.verify(
            base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4)),
            f"{header}.{claims}".encode(),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        self.assertEqual(json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4)))[
            "alg"
        ], "RS256")

    def test_a_key_that_loads_but_cannot_sign_raises_the_documented_error(self) -> None:
        # an EC or Ed25519 key parses, then a bare TypeError escapes from sign(),
        # and health() catches only AppAuthError, so the probe would raise
        from cryptography.hazmat.primitives.asymmetric import ec

        for material in (
            ec.generate_private_key(ec.SECP256R1()).private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            generate_ed25519_pem(),
        ):
            with self.subTest(material=material[:24]):
                with self.assertRaises(AppAuthError):
                    mint_jwt(CLIENT_ID, material)
                health = AppAuth(CLIENT_ID, material, now=lambda: 1000.0,
                                 transport=FakeTransport()).health()
                self.assertFalse(health["ok"])
                self.assertNotIn("BEGIN", str(health.get("error", "")))

    def test_a_missing_key_fails_rather_than_pretending_to_sign(self) -> None:
        with self.assertRaises(AppAuthError):
            mint_jwt(CLIENT_ID, b"")


class TokenTest(unittest.TestCase):
    def auth(self, transport) -> AppAuth:
        # the broker and the fake share one clock, so advancing time moves both
        transport.now = 1000.0
        return AppAuth(CLIENT_ID, generate_key(), now=lambda: transport.now, transport=transport)

    def test_a_token_is_minted_once_and_reused(self) -> None:
        transport = FakeTransport()
        auth = self.auth(transport)
        first = auth.token()
        # the clock has not moved, so the cached token is still fresh
        transport.now += TOKEN_REFRESH_MARGIN_SECONDS - 10
        self.assertEqual(auth.token(), first)
        self.assertEqual(transport.tokens_issued, 1)

    def test_a_token_is_reminted_before_it_expires(self) -> None:
        transport = FakeTransport()
        auth = self.auth(transport)
        first = auth.token()
        # a minted token lives an hour, so the cache is only handed out while
        # more than the refresh margin remains before that expiry
        transport.now += TOKEN_LIFETIME_SECONDS - TOKEN_REFRESH_MARGIN_SECONDS + 1
        self.assertNotEqual(auth.token(), first)
        self.assertEqual(transport.tokens_issued, 2)

    def test_a_token_is_still_handed_out_just_after_minting(self) -> None:
        transport = FakeTransport()
        auth = self.auth(transport)
        first = auth.token()
        transport.now += TOKEN_LIFETIME_SECONDS - TOKEN_REFRESH_MARGIN_SECONDS - 10
        self.assertEqual(auth.token(), first)
        self.assertEqual(transport.tokens_issued, 1)

    def test_a_missing_token_is_an_error_not_an_empty_credential(self) -> None:
        transport = FakeTransport()
        auth = self.auth(transport)

        def empty(path, token, body=None):
            return {} if path.endswith("/access_tokens") else transport(path, token, body)

        auth._transport = empty
        with self.assertRaises(AppAuthError):
            auth.token()

    def test_no_request_ever_carries_the_private_key(self) -> None:
        key = generate_key()
        transport = FakeTransport()
        AppAuth(CLIENT_ID, key, now=lambda: 1000.0, transport=transport).repositories()
        pem = key.decode()
        self.assertTrue(transport.calls)
        for path, credential, _ in transport.calls:
            self.assertNotIn("PRIVATE KEY", credential)
            self.assertNotIn("BEGIN", credential)
            # neither does any part of the key, which is not the PEM header
            for line in pem.splitlines()[1:-1]:
                self.assertNotIn(line[:24], credential)
            self.assertNotIn(pem.strip(), path)


class InstallationTest(unittest.TestCase):
    def auth(self, transport) -> AppAuth:
        return AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)

    def test_a_suspended_installation_is_refused(self) -> None:
        def suspended(path, token, body=None):
            return [
                {"id": 1, "account": {"login": "example-org"}, "repository_selection": "all",
                 "suspended_at": "2026-01-01T00:00:00Z"}
            ]

        with self.assertRaises(AppAuthError) as caught:
            self.auth(suspended).installation()
        self.assertIn("suspended", str(caught.exception))

    def test_no_installation_is_refused_rather_than_serving_nothing(self) -> None:
        with self.assertRaises(AppAuthError) as caught:
            self.auth(lambda *a, **k: []).installation()
        self.assertIn("not installed", str(caught.exception))

    def test_several_installations_are_ambiguous_and_said_so(self) -> None:
        def two(path, token, body=None):
            return [
                {"id": 1, "account": {"login": "a"}, "repository_selection": "all", "suspended_at": None},
                {"id": 2, "account": {"login": "b"}, "repository_selection": "all", "suspended_at": None},
            ]

        with self.assertRaises(AppAuthError) as caught:
            self.auth(two).installation()
        self.assertIn("a:1", str(caught.exception))


class RepositoryDiscoveryTest(unittest.TestCase):
    def test_pages_are_followed_until_a_short_page(self) -> None:
        pages = {
            1: {"repositories": [{"full_name": f"example-org/r{i}"} for i in range(2)]},
            2: {"repositories": [{"full_name": "example-org/last"}]},
        }

        def transport(path, token, body=None):
            if path.startswith("/installation/repositories"):
                return pages[int(path.rsplit("page=", 1)[1])]
            return FakeTransport()(path, token, body)

        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        self.assertEqual(auth.repositories(per_page=2), ["example-org/r0", "example-org/r1", "example-org/last"])

    def test_a_full_page_at_the_cap_raises_instead_of_truncating(self) -> None:
        # a silent prefix is a set of repositories nobody chose to leave out
        def transport(path, token, body=None):
            if path.startswith("/installation/repositories"):
                return {"repositories": [{"full_name": f"example-org/r{i}"} for i in range(3)]}
            return FakeTransport()(path, token, body)

        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        with self.assertRaises(AppAuthError) as caught:
            auth.repositories(per_page=3, max_pages=2)
        self.assertIn("6 repositories", str(caught.exception))


class HealthTest(unittest.TestCase):
    def test_health_reports_the_installation_and_never_the_token(self) -> None:
        transport = FakeTransport()
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        token = auth.token()
        health = auth.health()
        self.assertTrue(health["ok"])
        self.assertEqual(health["installation"], 165836227)
        self.assertEqual(health["repository_selection"], "all")
        self.assertNotIn(token, json.dumps(health))

    def test_health_reports_a_failure_instead_of_raising(self) -> None:
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=lambda *a, **k: [])
        health = auth.health()
        self.assertFalse(health["ok"])
        self.assertIn("not installed", health["error"])

    def test_repr_keeps_the_key_and_the_token_out_of_a_log_line(self) -> None:
        transport = FakeTransport()
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        token = auth.token()
        printed = repr(auth)
        self.assertNotIn(token, printed)
        self.assertNotIn("PRIVATE", printed)
        self.assertIn(CLIENT_ID, printed)


class RealRequestTest(unittest.TestCase):
    """_request over a socket, because it is the only place a token is real."""

    def serve(self, handler_body, status=200):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        seen: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self):
                seen.append((self.path, self.headers.get("Authorization", "")))
                payload = handler_body(self.path).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _reply
            do_POST = _reply

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        server.block_on_close = False
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(close)
        return f"http://127.0.0.1:{server.server_port}", seen

    def test_a_rejected_credential_never_echoes_the_github_error_body(self) -> None:
        # a GitHub error body can quote the credential it rejected, and this
        # message may be logged, so only the status and the path survive
        def body(_path):
            return '{"message": "Bad credentials for ghs_secretvalue0000000", "token": "ghs_secretvalue0000000"}'

        base, seen = self.serve(body, status=403)
        auth = AppAuth(CLIENT_ID, generate_key(), api_url=base, now=lambda: 1000.0)
        with self.assertRaises(AppAuthError) as caught:
            auth.installations()
        self.assertIn("403", str(caught.exception))
        self.assertNotIn("ghs_", str(caught.exception))
        self.assertNotIn("Bad credentials", str(caught.exception))
        # the credential was sent, and only as a bearer header
        self.assertTrue(seen)
        self.assertTrue(all(header.startswith("Bearer ey") for _, header in seen))
        self.assertTrue(all("PRIVATE" not in header for _, header in seen))

    def test_an_unreachable_github_reports_the_path_not_the_exception(self) -> None:
        auth = AppAuth(CLIENT_ID, generate_key(), api_url="http://127.0.0.1:1", now=lambda: 1000.0)
        with self.assertRaises(AppAuthError) as caught:
            auth.installations()
        self.assertIn("/app/installations", str(caught.exception))
        self.assertNotIn("BEGIN", str(caught.exception))


class PagingAndScopeTest(unittest.TestCase):
    def test_installations_past_one_page_are_not_hidden(self) -> None:
        # an App on more accounts than a page holds would otherwise look
        # unambiguous, and the broker would pick whichever the first page named
        transport = FakeTransport(installations=[
            {"id": index, "account": {"login": f"acct{index}"},
             "repository_selection": "all", "suspended_at": None}
            for index in range(1, 46)
        ])
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        found = auth.installations(per_page=20)
        self.assertEqual(len(found), 45)
        pages = [call[0] for call in transport.calls]
        self.assertEqual(pages, [
            "/app/installations?per_page=20&page=1",
            "/app/installations?per_page=20&page=2",
            "/app/installations?per_page=20&page=3",
        ])

    def test_more_installations_than_the_bound_is_refused_not_truncated(self) -> None:
        transport = FakeTransport(installations=[
            {"id": index, "account": {"login": f"acct{index}"},
             "repository_selection": "all", "suspended_at": None}
            for index in range(1, 6)
        ])
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        with self.assertRaises(AppAuthError) as caught:
            auth.installations(per_page=1, max_pages=3)
        self.assertIn("at least 3", str(caught.exception))

    def test_an_installation_scoped_to_nothing_is_refused(self) -> None:
        # serving it would accept every push and dispatch none of them
        transport = FakeTransport(
            installations=[{"id": 7, "account": {"login": "example-org"},
                            "repository_selection": "selected", "suspended_at": None}],
            repositories=[],
        )
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        with self.assertRaises(AppAuthError) as caught:
            auth.installation()
        self.assertIn("covers none", str(caught.exception))
        self.assertFalse(auth.health()["ok"])

    def test_a_refused_installation_leaves_no_usable_token(self) -> None:
        # the coverage check mints a token, and a refusal that left it cached
        # would hand a live credential to the next caller for the exact
        # installation the module just refused
        transport = FakeTransport(
            installations=[{"id": 7, "account": {"login": "example-org"},
                            "repository_selection": "selected", "suspended_at": None}],
            repositories=[],
        )
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        with self.assertRaises(AppAuthError):
            auth.installation()
        self.assertIsNone(auth._installation)
        self.assertIsNone(auth._token)
        self.assertEqual(auth._token_expires_at, 0.0)
        with self.assertRaises(AppAuthError):
            auth.token()
        self.assertFalse(auth.health()["ok"])

    def test_a_transient_failure_during_the_check_also_clears_the_token(self) -> None:
        # a URLError after the token is cached leaves it behind on the same path
        class Unreachable(FakeTransport):
            def __call__(self, path, token, body=None):
                if path.startswith("/installation/repositories"):
                    from urllib.error import URLError

                    raise URLError("down")
                return super().__call__(path, token, body)

        transport = Unreachable(
            installations=[{"id": 7, "account": {"login": "example-org"},
                            "repository_selection": "selected", "suspended_at": None}],
        )
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        with self.assertRaises(Exception):
            auth.installation()
        self.assertIsNone(auth._token)
        self.assertIsNone(auth._installation)

    def test_the_first_call_can_be_a_token_mint_on_a_scoped_installation(self) -> None:
        # token() -> installation() -> repositories() -> token() is the cycle that
        # needs the reentrant lock; with a plain Lock this hangs the thread
        # silently rather than failing a test
        transport = FakeTransport(
            installations=[{"id": 7, "account": {"login": "example-org"},
                            "repository_selection": "selected", "suspended_at": None}],
            repositories=[{"full_name": "example-org/cindral"}],
        )
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        # on a worker, so a non-reentrant lock fails this test by name instead of
        # wedging the interpreter until CI kills it
        done = threading.Event()
        minted: list[str] = []
        worker = threading.Thread(
            target=lambda: (minted.append(auth.token()), done.set()), daemon=True
        )
        worker.start()
        self.assertTrue(
            done.wait(5), "token() did not return: the lock is not reentrant"
        )
        self.assertEqual(minted, ["ghs_token_1"])
        self.assertEqual(auth.installation().id, 7)
        self.assertEqual(transport.tokens_issued, 1)

    def test_an_installation_entry_without_a_usable_id_is_refused_not_raised(self) -> None:
        for entry in ({"account": {"login": "example-org"}}, {"id": "seven"}, {"id": True}):
            with self.subTest(entry=entry):
                transport = FakeTransport(installations=[entry])
                auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
                self.assertFalse(auth.health()["ok"])
                with self.assertRaises(AppAuthError):
                    auth.installation()

    def test_an_installation_with_a_null_account_is_accepted_and_named_empty(self) -> None:
        # a shape real installations produce; health() catches only AppAuthError.
        # The name must be empty rather than the string "None", which is what a
        # truthy branch on the account produces.
        transport = FakeTransport(installations=[
            {"id": 7, "account": None, "repository_selection": "all", "suspended_at": None},
        ])
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        self.assertEqual(auth.installation().id, 7)
        self.assertEqual(auth.installation().account, "")
        self.assertTrue(auth.health()["ok"])
        self.assertNotIn("None", str(auth.health()))

    def test_an_unrecognised_selection_is_treated_as_restricted(self) -> None:
        # the gate was `== "selected"`, so a value this module does not know
        # skipped the probe and an installation covering nothing was served
        for value in ("Selected", "SELECTED", "partial", "", None, ["selected"]):
            with self.subTest(selection=value):
                transport = FakeTransport(
                    installations=[{"id": 7, "account": {"login": "example-org"},
                                    "repository_selection": value, "suspended_at": None}],
                    repositories=[],
                )
                auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
                with self.assertRaises(AppAuthError) as caught:
                    auth.installation()
                self.assertIn("covers none", str(caught.exception))

    def test_an_unrestricted_installation_still_skips_the_probe(self) -> None:
        transport = FakeTransport(
            installations=[{"id": 7, "account": {"login": "example-org"},
                            "repository_selection": "all", "suspended_at": None}],
            repositories=[],
        )
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        self.assertEqual(auth.installation().id, 7)

    def test_a_malformed_page_reports_a_failure_rather_than_raising(self) -> None:
        # the transport is what _request hands over, which is parsed JSON, so a
        # wrong shape arrives as a list or a string rather than as bytes. An
        # AttributeError out of here is not caught by health(), which would take
        # /healthz down with a traceback instead of reporting a failure.
        for page in ("repos", [], None, [1, 2], 7):
            with self.subTest(page=page):
                def transport(path, token, body=None, _page=page):
                    if path.endswith("/access_tokens"):
                        return {"token": "ghs_x", "expires_at": "2026-09-29T02:00:00Z"}
                    if path.startswith("/installation/repositories"):
                        return _page
                    return [{"id": 7, "account": {"login": "example-org"},
                             "repository_selection": "all", "suspended_at": None}]

                # reached through repositories(), which is the public walk; the
                # startup probe is skipped for an unrestricted installation
                auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
                with self.assertRaises(AppAuthError) as caught:
                    auth.repositories()
                self.assertIn("invalid", str(caught.exception))

    def test_a_malformed_installation_entry_reports_a_failure(self) -> None:
        for entries in ([ "a" ], [None], [3]):
            with self.subTest(entries=entries):
                transport = FakeTransport(installations=entries)
                auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
                self.assertFalse(auth.health()["ok"])

    def test_a_concurrent_caller_cannot_take_an_installation_mid_check(self) -> None:
        # the cache is populated for the duration of the coverage check, so a
        # second request thread that reads it before the check passes would hold
        # the very installation this is about to refuse
        import threading

        checking = threading.Event()
        release = threading.Event()
        outcomes: list[str] = []

        class Blocking(FakeTransport):
            def __call__(self, path, token, body=None):
                if path.startswith("/installation/repositories") and not checking.is_set():
                    checking.set()
                    release.wait(5)
                return super().__call__(path, token, body)

        transport = Blocking(
            installations=[{"id": 7, "account": {"login": "example-org"},
                            "repository_selection": "selected", "suspended_at": None}],
            repositories=[],
        )
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)

        def first():
            try:
                auth.installation()
                outcomes.append("resolved")
            except AppAuthError:
                outcomes.append("refused")

        thread = threading.Thread(target=first)
        thread.start()
        self.assertTrue(checking.wait(5))
        try:
            # the window the lock closes: without it, this returns the very
            # installation the first thread is about to refuse
            with self.assertRaises(AppAuthError):
                auth.installation()
        finally:
            release.set()
            thread.join(timeout=10)
        self.assertEqual(outcomes, ["refused"])

    def test_a_scoped_installation_covering_something_is_accepted(self) -> None:
        transport = FakeTransport(
            installations=[{"id": 7, "account": {"login": "example-org"},
                            "repository_selection": "selected", "suspended_at": None}],
            repositories=[{"full_name": "example-org/cindral"}],
        )
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        self.assertEqual(auth.installation().id, 7)

    def test_a_cold_burst_of_callers_mints_one_token(self) -> None:
        # the broker runs a thread per request, and without the lock every
        # thread of a cold burst minted its own, multiplying the slowest call
        import threading

        transport = FakeTransport()
        auth = AppAuth(CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=transport)
        barrier = threading.Barrier(8)
        results: list[str] = []

        def take():
            barrier.wait(5)
            results.append(auth.token())

        threads = [threading.Thread(target=take) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        self.assertEqual(transport.tokens_issued, 1)
        self.assertEqual(len(results), 8)
        self.assertEqual(len(set(results)), 1)

    def test_a_stated_expiry_beyond_the_lifetime_is_clamped(self) -> None:
        self.assertEqual(_expiry("2099-01-01T00:00:00Z", 1000.0), 4600.0)
        self.assertEqual(_expiry("not a date", 1000.0), 4600.0)
        self.assertEqual(_expiry(None, 1000.0), 4600.0)
        self.assertEqual(_expiry("1970-01-01T00:16:40Z", 1000.0), 1000.0)
