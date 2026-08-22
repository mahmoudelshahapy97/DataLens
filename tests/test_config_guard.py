"""The startup guard: a dangerous configuration must not reach the point of serving.

Almost every serious weakness in the original stack was a permissive default that
nobody chose. Each was documented in a comment beside the code that did it, and each
was defaulted correctly in the compose file -- which protects exactly one deployment
path and no other.

These tests assert the *refusal*, not the comment.
"""

from __future__ import annotations

import pytest

from vanna_app.config import (
    MULTI_TENANT,
    SINGLE_TENANT,
    ConfigError,
    load_and_validate,
    load_settings,
    validate,
)


class TestMultiTenantRefusals:
    """Every one of these was a live permissive default before."""

    def test_empty_admin_list_is_refused(self, env):
        # The headline case: `not ADMIN_EMAILS or email in ADMIN_EMAILS` made every
        # authenticated user a platform admin of every workspace.
        env["VANNA_ADMIN_EMAILS"] = ""
        with pytest.raises(ConfigError) as caught:
            load_and_validate(env)
        assert "VANNA_ADMIN_EMAILS" in str(caught.value)

    def test_missing_control_plane_is_refused(self, env):
        env["VANNA_APP_DATABASE_URL"] = ""
        with pytest.raises(ConfigError) as caught:
            load_and_validate(env)
        assert "VANNA_APP_DATABASE_URL" in str(caught.value)

    def test_insecure_cookies_are_refused(self, env):
        env["VANNA_SECURE_COOKIES"] = "false"
        with pytest.raises(ConfigError) as caught:
            load_and_validate(env)
        assert "VANNA_SECURE_COOKIES" in str(caught.value)

    def test_header_authentication_is_refused(self, env):
        env["VANNA_TRUST_HEADERS"] = "true"
        with pytest.raises(ConfigError) as caught:
            load_and_validate(env)
        assert "X-User-Email" in str(caught.value)

    def test_anonymous_access_is_refused(self, env):
        env["VANNA_ALLOW_ANONYMOUS"] = "true"
        with pytest.raises(ConfigError):
            load_and_validate(env)

    def test_public_roster_is_refused(self, env):
        env["VANNA_PUBLIC_USER_DIRECTORY"] = "true"
        with pytest.raises(ConfigError) as caught:
            load_and_validate(env)
        assert "roster" in str(caught.value).lower()

    def test_missing_secret_key_is_refused(self, env):
        env["VANNA_SECRET_KEY"] = ""
        with pytest.raises(ConfigError) as caught:
            load_and_validate(env)
        assert "VANNA_SECRET_KEY" in str(caught.value)

    def test_short_secret_key_is_refused(self, env):
        env["VANNA_SECRET_KEY"] = "tooshort"
        with pytest.raises(ConfigError):
            load_and_validate(env)

    def test_wildcard_cors_is_refused(self, env):
        env["VANNA_CORS_ORIGINS"] = "*"
        with pytest.raises(ConfigError) as caught:
            load_and_validate(env)
        assert "wildcard" in str(caught.value).lower() or "*" in str(caught.value)

    def test_no_trusted_proxies_is_refused(self, env):
        # Without this every request appears to come from the reverse proxy, so the
        # per-IP login throttle would lock out every user at once.
        env["VANNA_TRUSTED_PROXIES"] = ""
        with pytest.raises(ConfigError) as caught:
            load_and_validate(env)
        assert "VANNA_TRUSTED_PROXIES" in str(caught.value)

    def test_a_correct_configuration_starts(self, env):
        settings = load_and_validate(env)
        assert settings.is_multi_tenant
        assert settings.admin_emails == {"root@example.com"}


class TestEveryProblemIsReported:
    def test_all_faults_are_listed_at_once(self, env):
        """One restart per fault is how a deployment takes an afternoon."""
        env.update({
            "VANNA_ADMIN_EMAILS": "",
            "VANNA_SECURE_COOKIES": "false",
            "VANNA_SECRET_KEY": "",
        })
        with pytest.raises(ConfigError) as caught:
            load_and_validate(env)
        assert len(caught.value.problems) >= 3


class TestModes:
    def test_demo_permits_everything(self):
        settings = load_and_validate({"VANNA_DEPLOYMENT_MODE": "demo"})
        assert settings.is_demo
        assert not settings.admin_emails
        assert validate(settings) == []

    def test_single_tenant_still_needs_a_way_to_authenticate(self):
        with pytest.raises(ConfigError) as caught:
            load_and_validate({"VANNA_DEPLOYMENT_MODE": SINGLE_TENANT})
        assert "authenticate" in str(caught.value)

    def test_single_tenant_accepts_explicit_anonymous(self):
        settings = load_and_validate({
            "VANNA_DEPLOYMENT_MODE": SINGLE_TENANT,
            "VANNA_ALLOW_ANONYMOUS": "true",
        })
        assert settings.allow_anonymous

    def test_an_unknown_mode_fails_closed(self):
        """A typo in the mode name must not be a route into demo mode."""
        settings = load_settings({"VANNA_DEPLOYMENT_MODE": "multitenant"})
        assert settings.mode == MULTI_TENANT

    def test_secret_key_required_whenever_there_is_a_control_plane(self):
        with pytest.raises(ConfigError) as caught:
            load_and_validate({
                "VANNA_DEPLOYMENT_MODE": SINGLE_TENANT,
                "VANNA_APP_DATABASE_URL": "postgresql://u:p@h/db",
            })
        assert "VANNA_SECRET_KEY" in str(caught.value)


class TestReaders:
    def test_unrecognised_boolean_falls_back_to_the_default(self):
        # `VANNA_SECURE_COOKIES=yes` silently meaning False is the class of surprise
        # the reader exists to remove; it warns and uses the default.
        settings = load_settings({"VANNA_SECURE_COOKIES": "maybe"})
        assert settings.secure_cookies is False

    def test_boolean_spellings_people_actually_type(self):
        for raw in ("true", "TRUE", "1", "yes", "on"):
            assert load_settings({"VANNA_SECURE_COOKIES": raw}).secure_cookies is True
        for raw in ("false", "0", "no", "off"):
            assert load_settings({"VANNA_SECURE_COOKIES": raw}).secure_cookies is False

    def test_malformed_cidr_is_dropped_not_accepted(self):
        settings = load_settings({"VANNA_TRUSTED_PROXIES": "10.0.0.0/8,not-a-network"})
        assert len(settings.trusted_proxies) == 1

    def test_numbers_below_the_minimum_are_clamped(self):
        assert load_settings({"VANNA_MAX_ROWS": "0"}).max_rows == 1

    def test_admin_emails_are_lowercased(self):
        settings = load_settings({"VANNA_ADMIN_EMAILS": "Root@Example.COM, b@x.io"})
        assert settings.admin_emails == {"root@example.com", "b@x.io"}


class TestRedaction:
    def test_secrets_never_appear_in_the_redacted_view(self, settings):
        view = settings.redacted()
        assert view["secret_key"] == "***"
        assert view["app_database_url"] == "***"
        # And the non-secret fields survive, or the view would be useless.
        assert view["mode"] == MULTI_TENANT
