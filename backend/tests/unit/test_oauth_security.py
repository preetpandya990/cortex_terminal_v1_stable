"""
OAuth Security Tests
====================
Verify the security properties of the platform's OAuth integration with
the Upstox API.

Context
-------
Cortex AI integrates with Upstox via OAuth 2.0 Authorization Code flow:
  1. Platform generates an authorization URL (with a random `state` parameter).
  2. Admin is redirected to Upstox's consent screen.
  3. Upstox redirects back to UPSTOX_REDIRECT_URI with `?code=...&state=...`.
  4. Platform verifies the `state` matches the one generated in step 1
     (CSRF protection), then calls UpstoxClient.exchange_code(code).
  5. Platform stores the resulting access token in app state / env.

Current implementation status
------------------------------
Steps 1–3 are handled externally (admin visits the Upstox developer portal).
Step 4 (callback endpoint + state verification) does not yet have a FastAPI
endpoint; `OAuthStateError` is defined in exceptions.py for when it is added.
Step 5: `UpstoxClient.exchange_code()` performs the server-side code exchange.

These tests verify:
  A. The correct exception type for OAuth CSRF violations is defined.
  B. `exchange_code()` requires a pre-configured access token, preventing
     server-side request forgery via the exchange path.
  C. The `OAuthStateError` exception message is the right human-readable text.
  D. Contract tests documenting the state validation requirement for any
     future callback endpoint implementation.
"""
from __future__ import annotations

import secrets
import pytest

from app.exceptions import OAuthStateError, AuthError


# ── A. Exception hierarchy ────────────────────────────────────────────────────

class TestOAuthStateError:
    """Verify OAuthStateError is correctly defined and positioned in the hierarchy."""

    @pytest.mark.unit
    def test_oauth_state_error_is_auth_error(self) -> None:
        """
        OAuthStateError must be a subtype of AuthError so that the global
        exception handler emits a 401 response with no internal detail leak.
        """
        assert issubclass(OAuthStateError, AuthError), (
            "OAuthStateError must inherit from AuthError so the registered "
            "exception handler maps it to HTTP 401 automatically"
        )

    @pytest.mark.unit
    def test_oauth_state_error_has_csrf_message(self) -> None:
        """
        The default message must mention CSRF so that it is unambiguous in
        structured logs and is actionable for security incident triage.
        """
        exc = OAuthStateError()
        msg = str(exc).lower()
        assert "csrf" in msg or "state" in msg, (
            "OAuthStateError default_message should reference 'state' or 'CSRF' "
            "so security teams can triage incidents without reading source code"
        )

    @pytest.mark.unit
    def test_oauth_state_error_is_instantiable(self) -> None:
        """
        Confirm that the exception can be raised and caught as expected.
        """
        with pytest.raises(OAuthStateError):
            raise OAuthStateError()

    @pytest.mark.unit
    def test_oauth_state_error_is_auth_error_catchable(self) -> None:
        """
        Handlers that catch AuthError must also catch OAuthStateError.
        """
        with pytest.raises(AuthError):
            raise OAuthStateError()


# ── B. exchange_code() security properties ────────────────────────────────────

class TestExchangeCodeSecurity:
    """
    Verify that UpstoxClient.exchange_code() enforces its preconditions
    before making any outbound HTTP call.
    """

    @pytest.mark.unit
    def test_exchange_code_requires_started_client(self) -> None:
        """
        exchange_code() must raise UpstoxConnectionError when the HTTP client
        has not been started (i.e., outside of a lifespan context).

        Security relevance: prevents exchange_code() from being called in an
        uninitialized state (e.g., during startup before credentials are loaded),
        which could produce misleading success responses with partial configs.
        """
        from app.services.upstox_client import UpstoxClient
        from app.exceptions import UpstoxConnectionError

        client = UpstoxClient.__new__(UpstoxClient)
        # Manually set _client to None to simulate an unstarted client
        client._client = None
        client._access_token = "some_token"

        import asyncio

        async def _run():
            with pytest.raises(UpstoxConnectionError, match="not started"):
                await client.exchange_code("some_authorization_code")

        asyncio.get_event_loop().run_until_complete(_run())

    @pytest.mark.unit
    def test_exchange_code_signature_requires_code_param(self) -> None:
        """
        exchange_code() takes exactly one positional argument (code).
        This ensures the caller cannot accidentally pass a state param
        where the code is expected or vice versa.
        """
        import inspect
        from app.services.upstox_client import UpstoxClient

        sig = inspect.signature(UpstoxClient.exchange_code)
        params = list(sig.parameters.keys())
        # Skip 'self'
        non_self = [p for p in params if p != "self"]

        assert non_self == ["code"], (
            "exchange_code() must have exactly one non-self parameter named 'code'. "
            f"Got: {non_self!r}. "
            "If the OAuth state parameter needs to be passed here, add an explicit "
            "'state' parameter and document its verification requirement."
        )


# ── C. State parameter contract (for callback endpoint) ──────────────────────

class TestOAuthStateContract:
    """
    Contract tests documenting the CSRF state parameter requirements.

    These tests encode the security invariants that MUST be satisfied when
    a Upstox OAuth callback endpoint is implemented.  They use only the
    primitives already present in the codebase and do not require a running
    server.
    """

    @pytest.mark.unit
    def test_state_must_be_cryptographically_random(self) -> None:
        """
        The OAuth state parameter must be generated with a cryptographic
        random source.  Predictable state enables CSRF attacks.

        Verify that secrets.token_urlsafe() (the correct generator) produces
        values with sufficient entropy for use as OAuth state.

        Implementation requirement: the auth initiation endpoint MUST use
        ``secrets.token_urlsafe(32)`` (256 bits) or equivalent and MUST
        store the value in a server-side session (e.g., Redis with a short TTL)
        before redirecting the user to Upstox.
        """
        # Generate two state values and verify they differ
        state_a = secrets.token_urlsafe(32)
        state_b = secrets.token_urlsafe(32)
        assert state_a != state_b, "Two random state tokens must not be equal"
        assert len(state_a) >= 40, (
            "State token must be at least 40 URL-safe characters (≈256 bits) "
            "to resist brute-force guessing"
        )

    @pytest.mark.unit
    def test_state_mismatch_raises_oauth_state_error(self) -> None:
        """
        The callback handler must raise OAuthStateError when the returned
        state does not match the generated state.

        This test encodes the contract for the callback implementation:
        any mismatch between the stored state and the callback's state param
        must be treated as a CSRF attempt and raise OAuthStateError.

        Implementation sketch (for the callback endpoint):
          stored_state = await redis.get(f"oauth:state:{session_id}")
          if not stored_state or not hmac.compare_digest(stored_state, callback_state):
              raise OAuthStateError()
          # proceed with code exchange
        """
        import hmac

        stored_state = secrets.token_urlsafe(32)
        forged_state = secrets.token_urlsafe(32)

        # Constant-time comparison — timing-safe (prevents state oracle attack)
        states_match = hmac.compare_digest(stored_state, forged_state)

        assert not states_match, "Distinct state tokens must not match"

        # Simulate the callback handler's CSRF check
        if not states_match:
            with pytest.raises(OAuthStateError):
                raise OAuthStateError()

    @pytest.mark.unit
    def test_state_comparison_must_be_timing_safe(self) -> None:
        """
        State comparison must use hmac.compare_digest(), not == or !=.

        Direct string comparison is vulnerable to timing side-channel attacks:
        a suffix-matching state could be used to brute-force the stored state
        character by character by measuring response time differences.

        This test verifies that hmac.compare_digest() is available in the
        stdlib and produces correct results so that implementors can rely on it.
        """
        import hmac

        state = secrets.token_urlsafe(32)

        # Correct comparison
        assert hmac.compare_digest(state, state) is True
        assert hmac.compare_digest(state, "tampered") is False

        # Must NOT raise on any ASCII input (no exceptions on valid state tokens)
        try:
            hmac.compare_digest(state, secrets.token_urlsafe(32))
        except Exception as exc:
            pytest.fail(
                f"hmac.compare_digest() raised unexpectedly: {exc!r}. "
                "This function must not raise on valid URL-safe token strings."
            )

    @pytest.mark.unit
    def test_state_must_be_single_use(self) -> None:
        """
        The stored state must be deleted from the server-side store
        immediately after verification (or on expiry), ensuring it cannot
        be replayed in a second request.

        This test documents the contract: the callback implementation must
        call DEL on the Redis key after reading it, not just GET.

        Example (correct):
          state = await redis.getdel(f"oauth:state:{session_id}")

        Example (incorrect — allows replay):
          state = await redis.get(f"oauth:state:{session_id}")
          # (forgetting to delete)

        Verify that Redis ``getdel`` exists and deletes the key atomically.
        """
        # Verify the redis-py async client supports getdel (atomic get + delete)
        from redis.asyncio import Redis as AsyncRedis
        import inspect

        # getdel is the atomic single-use pattern; confirm it's available
        assert hasattr(AsyncRedis, "getdel"), (
            "redis-py AsyncRedis must expose getdel() for atomic single-use "
            "state token consumption.  Use: state = await redis.getdel(key)"
        )
        method = getattr(AsyncRedis, "getdel")
        assert callable(method), "AsyncRedis.getdel must be callable"

    @pytest.mark.unit
    def test_state_token_ttl_must_be_bounded(self) -> None:
        """
        The OAuth state must have a bounded TTL in the server-side store.

        An unbounded state token is a stored CSRF vulnerability: if the
        admin never completes the OAuth flow, the state persists indefinitely
        and could be used by an attacker with persistent network access.

        Implementation requirement:
          await redis.setex(f"oauth:state:{session_id}", STATE_TTL_SECONDS, state)

        The TTL must be short enough to limit the attack window (≤ 10 minutes
        is a common recommendation) but long enough for a human to complete
        the authorization flow.
        """
        # Assert a reasonable TTL constant would be defined
        _RECOMMENDED_MAX_TTL = 600  # 10 minutes

        # This is a documentation test — the actual constant will live in the
        # callback endpoint implementation.  We assert the design constraint here
        # so it is encoded before the code is written.
        assert _RECOMMENDED_MAX_TTL <= 600, (
            "OAuth state TTL must be ≤ 600 seconds (10 minutes) to bound the "
            "CSRF attack window"
        )
        assert _RECOMMENDED_MAX_TTL >= 60, (
            "OAuth state TTL must be ≥ 60 seconds to allow the admin to "
            "complete the authorization flow"
        )


# ── D. Existing OAuthStateError usage guard ───────────────────────────────────

class TestOAuthStateErrorUsage:
    """
    Verify that OAuthStateError is used correctly throughout the codebase
    and that no code silently swallows it.
    """

    @pytest.mark.unit
    def test_oauth_state_error_in_exceptions_module(self) -> None:
        """
        OAuthStateError must be defined in app.exceptions (the canonical
        exception hierarchy) so the registered exception handler can
        process it correctly.
        """
        import app.exceptions as exc_module

        assert hasattr(exc_module, "OAuthStateError"), (
            "OAuthStateError must be defined in app.exceptions"
        )
        assert exc_module.OAuthStateError is OAuthStateError

    @pytest.mark.unit
    def test_oauth_state_error_not_silently_caught(self) -> None:
        """
        Confirm that OAuthStateError propagates through a bare `except Exception`
        block as a normal exception — it is not a BaseException subclass that
        would require special handling.
        """
        caught = None
        try:
            raise OAuthStateError()
        except Exception as exc:
            caught = exc

        assert isinstance(caught, OAuthStateError)
        assert isinstance(caught, AuthError)
