"""Authorisation predicates.

The whole product rests on these six functions, and one of them used to read::

    is_platform_admin = (not ADMIN_EMAILS) or (email in ADMIN_EMAILS)

which granted everybody everything whenever a variable was unset. The tests below
exist so that expression cannot come back by accident.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from vanna_app.authz import (
    forbid_viewer,
    is_platform_admin,
    is_tenant_admin,
    require_full_session,
    require_platform_admin,
    require_tenant_admin,
    role_of,
    visible_tenant,
)


class TestPlatformAdmin:
    def test_named_addresses_are_admins(self, settings, user_factory):
        assert is_platform_admin(user_factory(email="root@example.com"), settings)

    def test_everybody_else_is_not(self, settings, user_factory):
        assert not is_platform_admin(user_factory(email="ada@acme.com"), settings)

    def test_an_empty_list_grants_nobody(self, settings, user_factory):
        """The inversion. This used to grant *everybody*."""
        stripped = type(settings)(**{**settings.__dict__, "admin_emails": set()})
        assert not is_platform_admin(user_factory(email="ada@acme.com"), stripped)
        assert not is_platform_admin(user_factory(email="root@example.com"), stripped)

    def test_demo_mode_grants_everybody_explicitly(self, demo_settings, user_factory):
        # A mode somebody chose, not a variable somebody forgot.
        assert is_platform_admin(user_factory(email="anyone@nowhere"), demo_settings)

    def test_matching_is_case_insensitive(self, settings, user_factory):
        assert is_platform_admin(user_factory(email="ROOT@Example.com"), settings)

    def test_a_workspace_admin_is_not_a_platform_admin(self, settings, user_factory):
        assert not is_platform_admin(user_factory(role="admin"), settings)


class TestTenantAdmin:
    def test_admin_of_own_workspace(self, settings, user_factory):
        assert is_tenant_admin(user_factory(role="admin", tenant_id="acme"), "acme", settings)

    def test_not_admin_of_another_workspace(self, settings, user_factory):
        assert not is_tenant_admin(user_factory(role="admin", tenant_id="acme"), "globex", settings)

    def test_analyst_is_not_an_admin(self, settings, user_factory):
        assert not is_tenant_admin(user_factory(role="analyst", tenant_id="acme"), "acme", settings)

    def test_platform_admin_administers_any_workspace(self, settings, user_factory):
        root = user_factory(email="root@example.com", tenant_id="acme", role="admin")
        assert is_tenant_admin(root, "globex", settings)


class TestGuards:
    def test_refusals_are_404_not_403(self, settings, user_factory):
        """403 confirms the resource exists to somebody with no business knowing."""
        with pytest.raises(HTTPException) as caught:
            require_platform_admin(user_factory(), settings)
        assert caught.value.status_code == 404

        with pytest.raises(HTTPException) as caught:
            require_tenant_admin(user_factory(tenant_id="acme"), "globex", settings)
        assert caught.value.status_code == 404

    def test_viewers_cannot_write(self, user_factory):
        with pytest.raises(HTTPException) as caught:
            forbid_viewer(user_factory(role="viewer"))
        # 403 here, deliberately: the caller is a member and already knows the
        # resource exists, so an accurate message saves a support ticket.
        assert caught.value.status_code == 403

    def test_analysts_and_admins_can_write(self, user_factory):
        forbid_viewer(user_factory(role="analyst"))
        forbid_viewer(user_factory(role="admin"))


class TestSessionScope:
    def test_a_password_change_session_is_refused_elsewhere(self, user_factory):
        restricted = user_factory(session_scope="password_change_only")
        with pytest.raises(HTTPException) as caught:
            require_full_session(restricted)
        assert caught.value.status_code == 403
        assert caught.value.detail["code"] == "password_change_required"

    def test_a_full_session_passes(self, user_factory):
        require_full_session(user_factory())


class TestVisibleTenant:
    def test_own_workspace_resolves(self, settings, user_factory):
        assert visible_tenant(user_factory(tenant_id="acme"), "acme", settings) == "acme"

    def test_no_request_means_own_workspace(self, settings, user_factory):
        assert visible_tenant(user_factory(tenant_id="acme"), None, settings) == "acme"

    def test_another_workspace_is_a_404(self, settings, user_factory):
        with pytest.raises(HTTPException) as caught:
            visible_tenant(user_factory(tenant_id="acme", role="admin"), "globex", settings)
        assert caught.value.status_code == 404

    def test_platform_admin_may_name_any_workspace(self, settings, user_factory):
        root = user_factory(email="root@example.com", tenant_id="acme")
        assert visible_tenant(root, "globex", settings) == "globex"


class TestRole:
    def test_role_comes_from_metadata_not_the_request(self, user_factory):
        assert role_of(user_factory(role="viewer")) == "viewer"

    def test_an_unknown_role_reads_as_analyst(self, user_factory):
        user = user_factory()
        user.metadata["role"] = "superuser"
        # Never falls back to something privileged.
        assert role_of(user) == "analyst"

    def test_a_missing_role_reads_as_analyst(self, user_factory):
        user = user_factory()
        user.metadata = {}
        assert role_of(user) == "analyst"
