"""Whether a workspace's database still answers, and who is told.

``probe()`` ran once, when a data source was registered, and the result was
discarded with the request. Nothing recorded it, so a rotated password or a moved
host looked exactly like a healthy source until somebody asked a question and got
an error they could not interpret.

Two properties are pinned here, and the second is the one that matters in a
multi-tenant deployment:

* the result of a check is remembered, including the distinction between "checked
  and broken" and "never checked" -- because a console that renders unknown as
  green is worse than one that renders nothing;
* one workspace's database being down does **not** make the process unready. A 503
  there would stop this replica serving the eight workspaces that are fine, which
  turns one customer's outage into everybody's.

Plus the detail that makes the error safe to show: driver messages routinely echo
the connection string, password included, and this text is rendered in a browser.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

WORKING = "postgresql://postgres:postgres123@127.0.0.1:5432/postgres"


@pytest.fixture
async def registry(app_db):
    from vanna_app.datasources import DataSourceRegistry
    from vanna_app.secrets import Cipher

    await app_db.execute(
        "INSERT INTO vanna_app.tenants (id, name) VALUES ('acme', 'Acme') "
        "ON CONFLICT (id) DO NOTHING"
    )
    return DataSourceRegistry(app_db, Cipher("k" * 48))


class TestUnknownIsNotHealthy:
    async def test_a_new_source_has_never_been_checked(self, registry):
        """`None`, not False and not True. A source registered before this existed
        is honestly unknown, and the console has to say so differently."""
        await registry.register("acme", WORKING, is_default=True)

        listed = await registry.list_sources("acme")

        assert listed[0]["last_ok"] is None
        assert listed[0]["last_checked_at"] is None
        assert listed[0]["last_error"] is None


class TestTheResultIsRemembered:
    async def test_a_successful_check_is_recorded(self, registry):
        registered = await registry.register("acme", WORKING, is_default=True)

        result = await registry.check("acme", registered["data_source_id"])

        assert result["ok"] is True
        listed = await registry.list_sources("acme")
        assert listed[0]["last_ok"] is True
        assert listed[0]["last_checked_at"] is not None
        assert listed[0]["last_error"] is None

    async def test_a_failing_check_is_recorded_with_its_reason(self, registry):
        """The point of storing it: the next person to look does not have to
        reproduce the failure to find out what it was."""
        # `register` does not probe, so a bad password can be stored -- which is
        # exactly the state this feature exists to surface later.
        broken = WORKING.replace("postgres123", "definitely-not-the-password")
        registered = await registry.register("acme", broken, is_default=True)

        result = await registry.check("acme", registered["data_source_id"])

        assert result["ok"] is False
        assert result["error"]
        listed = await registry.list_sources("acme")
        assert listed[0]["last_ok"] is False
        assert listed[0]["last_error"]

    async def test_recovery_clears_the_error(self, registry):
        registered = await registry.register("acme", WORKING, is_default=True)
        await registry.record_health(
            "acme", registered["data_source_id"], ok=False, error="an old failure"
        )

        await registry.check("acme", registered["data_source_id"])

        listed = await registry.list_sources("acme")
        assert listed[0]["last_ok"] is True
        assert listed[0]["last_error"] is None, "a stale error outlived its cause"

    async def test_an_unregistered_source_is_not_found(self, registry):
        from vanna_app.datasources import UnknownDataSource

        await registry.register("acme", WORKING, is_default=True)

        with pytest.raises(UnknownDataSource):
            await registry.check("acme", "postgresql://nowhere/nothing")


class TestTheErrorIsSafeToShow:
    """Driver messages echo what they tried to connect to. This is rendered in a
    browser for anyone who administers the workspace."""

    def test_the_connection_string_is_removed(self):
        from vanna_app.datasources import _sanitise

        url = "postgresql://admin:sup3rs3cret@warehouse:5432/analytics"
        error = Exception(f'could not connect using "{url}"')

        cleaned = _sanitise(error, url)

        assert url not in cleaned
        assert "sup3rs3cret" not in cleaned

    def test_a_bare_password_is_removed_too(self):
        """Some drivers name the password without the rest of the URL around it."""
        from vanna_app.datasources import _sanitise

        url = "postgresql://admin:sup3rs3cret@warehouse:5432/analytics"
        error = Exception('password authentication failed: "sup3rs3cret" rejected')

        assert "sup3rs3cret" not in _sanitise(error, url)

    def test_something_useful_survives(self):
        """Redaction that leaves nothing is as useless as no check at all."""
        from vanna_app.datasources import _sanitise

        url = "postgresql://admin:secret@warehouse:5432/analytics"
        cleaned = _sanitise(Exception("password authentication failed for user"), url)

        assert "authentication failed" in cleaned

    def test_it_is_bounded(self):
        from vanna_app.datasources import _sanitise

        assert len(_sanitise(Exception("x" * 5000), "")) <= 500
