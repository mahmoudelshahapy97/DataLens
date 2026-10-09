"""Credentials, sessions, throttling, quota and billing, against a real database.

Each class here corresponds to a defect that was live:

* a password change left every other session and API token working,
* an admin password reset silently erased the account's name,
* ``must_change`` was a flag only the browser honoured,
* quota and throttling were per-process, so the limit was the limit times the
  worker count,
* an admin was exempt from both, because the library default was never overridden.
"""

from __future__ import annotations

import asyncio

import pytest

from vanna_app.accounts import SCOPE_FULL, SCOPE_PASSWORD_CHANGE
from vanna_app.db import SCHEMA

pytestmark = pytest.mark.integration

PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "a-different-long-password"


@pytest.fixture
async def ada(accounts):
    await accounts.create("ada@example.com", PASSWORD, full_name="Ada Lovelace")
    return "ada@example.com"


class TestVerification:
    async def test_a_correct_password_verifies(self, accounts, ada):
        assert await accounts.verify(ada, PASSWORD) is not None

    async def test_a_wrong_password_does_not(self, accounts, ada):
        assert await accounts.verify(ada, "wrong") is None

    async def test_an_unknown_account_does_not(self, accounts):
        assert await accounts.verify("nobody@example.com", PASSWORD) is None

    async def test_a_disabled_account_does_not(self, accounts, ada):
        await accounts.set_active(ada, False)
        assert await accounts.verify(ada, PASSWORD) is None

    async def test_an_sso_account_cannot_be_signed_into_with_a_password(self, accounts):
        # password_hash is empty for an external identity; this asserts that an
        # empty hash is never treated as "any password will do".
        await accounts.upsert_external(
            "sso@example.com", provider="oidc", external_id="abc"
        )
        assert await accounts.verify("sso@example.com", "") is None
        assert await accounts.verify("sso@example.com", PASSWORD) is None

    async def test_the_email_is_matched_case_insensitively(self, accounts, ada):
        assert await accounts.verify("ADA@Example.COM", PASSWORD) is not None


class TestPasswordChangeRevokesEverything:
    async def test_other_sessions_end(self, accounts, ada):
        keep = await accounts.create_session(ada)
        other = await accounts.create_session(ada)

        await accounts.set_password(ada, NEW_PASSWORD, keep_token=keep)

        assert await accounts.session_user(keep) is not None
        assert await accounts.session_user(other) is None

    async def test_api_tokens_are_revoked(self, accounts, ada):
        token = await accounts.create_token(ada, name="laptop")
        assert await accounts.token_user(token) is not None

        await accounts.set_password(ada, NEW_PASSWORD)
        assert await accounts.token_user(token) is None

    async def test_outstanding_reset_links_are_burned(self, accounts, ada):
        reset = await accounts.create_reset(ada)
        await accounts.set_password(ada, NEW_PASSWORD)
        assert await accounts.redeem_reset(reset, "another-long-password") is None

    async def test_the_old_password_stops_working(self, accounts, ada):
        await accounts.set_password(ada, NEW_PASSWORD)
        assert await accounts.verify(ada, PASSWORD) is None
        assert await accounts.verify(ada, NEW_PASSWORD) is not None


class TestAdminReset:
    async def test_it_does_not_erase_the_name(self, accounts, ada):
        """The bug: `create()`'s upsert overwrote full_name with ''."""
        await accounts.set_temporary_password(ada, "temporary-password-123")
        row = await accounts.get(ada)
        assert row["full_name"] == "Ada Lovelace"

    async def test_it_forces_a_change(self, accounts, ada):
        await accounts.set_temporary_password(ada, "temporary-password-123")
        assert (await accounts.get(ada))["must_change"] is True

    async def test_it_ends_every_session(self, accounts, ada):
        session = await accounts.create_session(ada)
        await accounts.set_temporary_password(ada, "temporary-password-123")
        assert await accounts.session_user(session) is None


class TestSessionScope:
    async def test_a_restricted_session_reports_its_scope(self, accounts, ada):
        token = await accounts.create_session(ada, scope=SCOPE_PASSWORD_CHANGE)
        row = await accounts.session_user(token)
        assert row["session_scope"] == SCOPE_PASSWORD_CHANGE

    async def test_a_normal_session_is_full(self, accounts, ada):
        token = await accounts.create_session(ada)
        assert (await accounts.session_user(token))["session_scope"] == SCOPE_FULL

    async def test_an_expired_session_resolves_to_nobody(self, accounts, ada, app_db):
        token = await accounts.create_session(ada, ttl_hours=1)
        await app_db.execute(
            f"UPDATE {SCHEMA}.sessions SET expires_at = now() - interval '1 minute'"
        )
        assert await accounts.session_user(token) is None

    async def test_disabling_an_account_kills_its_sessions_immediately(self, accounts, ada):
        token = await accounts.create_session(ada)
        await accounts.set_active(ada, False)
        assert await accounts.session_user(token) is None


class TestEndingOneSession:
    """"Sign out everywhere else" was the only option, which is the wrong shape
    for the usual case: one unfamiliar device, and no reason to sign out the rest."""

    async def test_the_named_session_ends_and_the_others_live(self, accounts, ada):
        keep = await accounts.create_session(ada)
        doomed = await accounts.create_session(ada)
        other = await accounts.create_session(ada)
        listed = await accounts.list_sessions(ada, current_token=keep)
        target = next(
            row["id"] for row in listed
            if not row["is_current"] and is_session_for(row, doomed)
        )

        assert await accounts.delete_session(ada, target, keep_token=keep) == 1

        assert await accounts.session_user(doomed) is None
        assert await accounts.session_user(keep) is not None
        assert await accounts.session_user(other) is not None

    async def test_the_callers_own_session_is_refused(self, accounts, ada):
        """Killing it would sign the caller out while the page still believes it is
        signed in. ``/auth/logout`` is that operation and does the rest."""
        mine = await accounts.create_session(ada)
        listed = await accounts.list_sessions(ada, current_token=mine)
        current = next(row["id"] for row in listed if row["is_current"])

        assert await accounts.delete_session(ada, current, keep_token=mine) == 0
        assert await accounts.session_user(mine) is not None

    async def test_another_accounts_session_is_out_of_reach(self, accounts, ada):
        """The id is a hash prefix, not an authorisation, however it was obtained."""
        await accounts.create("bob@example.com", PASSWORD)
        theirs = await accounts.create_session("bob@example.com")
        listed = await accounts.list_sessions("bob@example.com")
        target = listed[0]["id"]

        assert await accounts.delete_session(ada, target) == 0
        assert await accounts.session_user(theirs) is not None

    async def test_a_malformed_id_ends_nothing(self, accounts, ada):
        session = await accounts.create_session(ada)

        for bogus in ("", "zz", "not-hex!", "0" * 64):
            assert await accounts.delete_session(ada, bogus) == 0
        assert await accounts.session_user(session) is not None


def is_session_for(row, token):
    """Whether this listed row is the session for *token*.

    ``list_sessions`` returns an eight-character hash prefix rather than the token,
    which is the whole point of it -- so matching a row back to a token a test holds
    means hashing the token the same way.
    """
    from vanna.core.auth import hash_token

    return hash_token(token).startswith(row["id"])


class TestPasswordReset:
    async def test_a_token_can_be_redeemed_once(self, accounts, ada):
        token = await accounts.create_reset(ada)
        assert await accounts.redeem_reset(token, NEW_PASSWORD) == ada
        assert await accounts.redeem_reset(token, "third-password-here") is None

    async def test_an_expired_token_is_refused(self, accounts, ada, app_db):
        token = await accounts.create_reset(ada)
        await app_db.execute(
            f"UPDATE {SCHEMA}.password_resets SET expires_at = now() - interval '1 minute'"
        )
        assert await accounts.redeem_reset(token, NEW_PASSWORD) is None

    async def test_an_unknown_address_yields_no_token(self, accounts):
        assert await accounts.create_reset("nobody@example.com") is None

    async def test_an_sso_account_yields_no_token(self, accounts):
        await accounts.upsert_external("sso@example.com", provider="oidc", external_id="x")
        assert await accounts.create_reset("sso@example.com") is None

    async def test_only_the_hash_is_stored(self, accounts, ada, app_db):
        token = await accounts.create_reset(ada)
        rows = await app_db.fetch_all(f"SELECT token_hash FROM {SCHEMA}.password_resets")
        assert token not in [r["token_hash"] for r in rows]


class TestApiTokens:
    async def test_a_token_authenticates(self, accounts, ada):
        token = await accounts.create_token(ada, name="ci")
        assert (await accounts.token_user(token))["email"] == ada

    async def test_only_the_hash_is_stored(self, accounts, ada, app_db):
        token = await accounts.create_token(ada)
        rows = await app_db.fetch_all(f"SELECT token_hash FROM {SCHEMA}.api_tokens")
        assert token not in [r["token_hash"] for r in rows]

    async def test_revocation_works_by_handle(self, accounts, ada):
        token = await accounts.create_token(ada, name="ci")
        handle = (await accounts.list_tokens(ada))[0]["id"]
        assert await accounts.revoke_token(ada, handle)
        assert await accounts.token_user(token) is None

    async def test_one_account_cannot_revoke_another_s_token(self, accounts, ada):
        await accounts.create("bob@example.com", PASSWORD)
        token = await accounts.create_token(ada, name="ci")
        handle = (await accounts.list_tokens(ada))[0]["id"]

        assert not await accounts.revoke_token("bob@example.com", handle)
        assert await accounts.token_user(token) is not None


# ----------------------------------------------------------------------
# Limits
# ----------------------------------------------------------------------


class TestCounters:
    async def test_hits_accumulate(self, counters):
        for expected in (1, 2, 3):
            assert await counters.hit("k", 60) == expected

    async def test_peek_does_not_record(self, counters):
        await counters.hit("k", 60)
        assert await counters.peek("k", 60) == 1
        assert await counters.peek("k", 60) == 1

    async def test_keys_are_independent(self, counters):
        await counters.hit("a", 60)
        assert await counters.peek("b", 60) == 0

    async def test_concurrent_hits_do_not_lose_any(self, counters):
        """The point of the whole exercise: two workers must agree on the count."""
        results = await asyncio.gather(*[counters.hit("race", 60) for _ in range(25)])
        assert sorted(results) == list(range(1, 26))

    async def test_clearing_forgets_a_key(self, counters):
        await counters.hit("k", 60)
        await counters.clear("k")
        assert await counters.peek("k", 60) == 0

    async def test_windows_are_separate_buckets(self, counters):
        await counters.hit("k", 60)
        # A different width is a different bucket, not a shared one.
        assert await counters.peek("k", 3600) == 0


class TestQuotaHook:
    @pytest.fixture
    def hook(self, counters):
        from vanna_app.limits import PostgresQuotaHook

        return PostgresQuotaHook(counters, max_messages=3, exempt_metadata_flag="byo_key")

    @staticmethod
    def _user(tenant="acme", groups=("user",), metadata=None):
        return type(
            "U", (), {
                "id": "ada", "tenant_id": tenant,
                "group_memberships": list(groups),
                "metadata": metadata or {},
            },
        )()

    async def test_the_limit_is_enforced(self, hook):
        from vanna.core.lifecycle.quota import QuotaExceededError

        user = self._user()
        for _ in range(3):
            assert await hook.before_message(user, "q") is None
        with pytest.raises(QuotaExceededError):
            await hook.before_message(user, "q")

    async def test_the_count_is_per_workspace_not_per_user(self, hook):
        from vanna.core.lifecycle.quota import QuotaExceededError

        for _ in range(3):
            await hook.before_message(self._user(), "q")
        # A different person, same workspace: the quota is sold per workspace and
        # the usage screen reports it that way.
        other = self._user()
        other.id = "bob"
        with pytest.raises(QuotaExceededError):
            await hook.before_message(other, "q")

    async def test_workspaces_do_not_share_a_budget(self, hook):
        for _ in range(3):
            await hook.before_message(self._user("acme"), "q")
        assert await hook.before_message(self._user("globex"), "q") is None

    async def test_an_admin_is_not_exempt(self, hook):
        """The library default exempts `admin`, and nothing overrode it."""
        from vanna.core.lifecycle.quota import QuotaExceededError

        admin = self._user(groups=("user", "admin"))
        for _ in range(3):
            await hook.before_message(admin, "q")
        with pytest.raises(QuotaExceededError):
            await hook.before_message(admin, "q")

    async def test_a_personal_key_is_exempt(self, hook):
        byo = self._user(metadata={"byo_key": True})
        for _ in range(10):
            assert await hook.before_message(byo, "q") is None

    async def test_it_fails_closed_when_the_counter_is_gone(self, counters, app_db):
        from vanna.core.lifecycle.quota import QuotaExceededError
        from vanna_app.limits import PostgresQuotaHook

        hook = PostgresQuotaHook(counters, max_messages=10)
        app_db.close()
        with pytest.raises(QuotaExceededError) as caught:
            await hook.before_message(self._user(), "q")
        # Quota bounds spend, and spend is what cannot be undone afterwards.
        assert "cannot be checked" in str(caught.value)


class TestRateLimitHook:
    async def test_it_fails_open_when_the_counter_is_gone(self, counters, app_db):
        """Load, not cost: a blip must not become a total refusal of service."""
        from vanna_app.limits import PostgresRateLimitHook

        hook = PostgresRateLimitHook(counters, max_requests=1)
        app_db.close()
        user = type("U", (), {"id": "a", "tenant_id": "t", "group_memberships": []})()
        assert await hook.before_message(user, "q") is None


class TestLoginThrottle:
    @pytest.fixture
    def throttle(self, counters):
        from vanna_app.limits import LoginThrottle

        return LoginThrottle(counters, max_attempts=3, window_seconds=300)

    async def test_it_blocks_after_the_budget(self, throttle):
        for _ in range(3):
            assert not await throttle.blocked("ada@x.io", "1.2.3.4")
            await throttle.record_failure("ada@x.io", "1.2.3.4")
        assert await throttle.blocked("ada@x.io", "1.2.3.4")

    async def test_the_address_and_the_ip_are_separate_budgets(self, throttle):
        for _ in range(3):
            await throttle.record_failure("ada@x.io", "1.2.3.4")
        # Same attacker, different account: the IP budget is already spent.
        assert await throttle.blocked("bob@x.io", "1.2.3.4")
        # Different network, untouched account: unaffected.
        assert not await throttle.blocked("bob@x.io", "5.6.7.8")

    async def test_success_clears_the_address_but_not_the_network(self, throttle):
        for _ in range(3):
            await throttle.record_failure("ada@x.io", "1.2.3.4")
        await throttle.clear("ada@x.io")
        # A shared office address that has just had one successful login should not
        # reset the budget for whoever else is working through a password list.
        assert await throttle.blocked("ada@x.io", "1.2.3.4")
        assert not await throttle.blocked("ada@x.io", "9.9.9.9")


# ----------------------------------------------------------------------
# Billing
# ----------------------------------------------------------------------


class TestBilling:
    @pytest.fixture
    async def tenant(self, directory):
        await directory.create_tenant("acme", name="Acme")
        return "acme"

    async def test_a_payment_reference_is_idempotent(self, billing, tenant):
        assert await billing.record_payment(
            tenant, provider="manual", provider_ref="inv-1", amount_cents=1000
        )
        # The replayed webhook, or the double-clicked button.
        assert not await billing.record_payment(
            tenant, provider="manual", provider_ref="inv-1", amount_cents=1000
        )
        assert len(await billing.list_payments(tenant)) == 1

    async def test_the_newest_subscription_wins(self, billing, tenant):
        await billing.set_subscription(tenant, "free")
        await billing.set_subscription(tenant, "pro")
        assert (await billing.get_subscription(tenant))["plan"] == "pro"

    async def test_history_is_kept(self, billing, tenant, app_db):
        await billing.set_subscription(tenant, "free")
        await billing.set_subscription(tenant, "pro")
        rows = await app_db.fetch_all(
            f"SELECT plan FROM {SCHEMA}.subscriptions WHERE tenant_id = %s", (tenant,)
        )
        assert len(rows) == 2

    async def test_an_unknown_plan_is_refused(self, billing, tenant):
        with pytest.raises(ValueError):
            await billing.set_subscription(tenant, "platinum")

    async def test_extending_early_does_not_lose_the_remaining_time(self, billing, tenant):
        await billing.set_subscription(tenant, "pro", months=1)
        before = (await billing.get_subscription(tenant))["expires_at"]
        await billing.extend(tenant, 1)
        after = (await billing.get_subscription(tenant))["expires_at"]
        assert after > before

    async def test_cancelling_keeps_the_row(self, billing, tenant):
        await billing.set_subscription(tenant, "pro")
        assert await billing.cancel_subscription(tenant)
        subscription = await billing.get_subscription(tenant)
        assert subscription["status"] == "cancelled"
        assert subscription["cancelled_at"] is not None


class TestPlanResolution:
    """The resolver is pure, but its interaction with stored rows is not."""

    async def test_an_expired_subscription_falls_back_to_free_not_to_zero(
        self, billing, directory, app_db
    ):
        from vanna.core.billing import resolve_limits

        await directory.create_tenant("acme", name="Acme")
        await billing.set_subscription("acme", "pro", months=1)
        await app_db.execute(
            f"UPDATE {SCHEMA}.subscriptions SET expires_at = now() - interval '1 day'"
        )

        limits = resolve_limits(
            await directory.get_tenant("acme"),
            await billing.get_subscription("acme"),
            default_quota=200,
            default_max_rows=1000,
        )
        # Losing access to your own data because a card expired is a support
        # incident, not a business model.
        assert limits.plan.name == "free"
        assert limits.daily_quota > 0

    async def test_an_explicit_override_beats_the_plan(self, billing, directory):
        from vanna.core.billing import resolve_limits

        await directory.create_tenant("acme", name="Acme", daily_quota=42)
        await billing.set_subscription("acme", "pro")
        limits = resolve_limits(
            await directory.get_tenant("acme"),
            await billing.get_subscription("acme"),
            default_quota=200,
            default_max_rows=1000,
        )
        assert limits.daily_quota == 42
        assert limits.quota_source == "override"
