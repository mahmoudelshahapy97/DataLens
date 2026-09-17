"""`ReportRunner.user_for` -- the identity a scheduled report executes as.

Before this file, `vanna_app/report_runner.py` (506 lines) had zero test
coverage of any kind. `user_for` is the highest-value piece to cover in
isolation: it is the one thing standing between "a departed employee's
schedule keeps mailing their old team the data they used to be able to see"
and the fail-closed behavior the module's own docstring promises. It only
touches `self.directory` (duck-typed: `get_tenant`/`get_member`), so a fake
directory exercises it with no Postgres control plane needed.
"""

from __future__ import annotations

import pytest

from vanna_app.report_runner import ReportRunner

pytestmark = pytest.mark.asyncio


class _FakeDirectory:
    def __init__(self, tenants=None, members=None):
        self._tenants = tenants or {}
        self._members = members or {}

    async def get_tenant(self, tenant_id):
        return self._tenants.get(tenant_id)

    async def get_member(self, tenant_id, email):
        return self._members.get((tenant_id, email))


def _runner(directory) -> ReportRunner:
    return ReportRunner(
        store=None, directory=directory, platform=None, deliver=None, settings=None
    )


class TestActiveMember:
    async def test_resolves_the_named_member_with_their_current_role(self):
        directory = _FakeDirectory(
            tenants={"acme": {"is_active": True}},
            members={
                ("acme", "ada@acme.test"): {
                    "role": "analyst",
                    "is_active": True,
                    "full_name": "Ada",
                }
            },
        )

        user = await _runner(directory).user_for("acme", "ada@acme.test")

        assert user.email == "ada@acme.test"
        assert user.tenant_id == "acme"
        assert user.metadata["role"] == "analyst"

    async def test_never_carries_platform_admin_even_for_a_platform_admins_email(self):
        """A report renders with only the grants the named member's *workspace
        role* carries -- never the elevated access that email might have as a
        platform admin in a live session, since nobody is asking here."""
        directory = _FakeDirectory(
            tenants={"acme": {"is_active": True}},
            members={
                ("acme", "root@example.com"): {
                    "role": "admin",
                    "is_active": True,
                    "full_name": "",
                }
            },
        )

        user = await _runner(directory).user_for("acme", "root@example.com")

        assert user.metadata["platform_admin"] is False
        assert user.metadata["session_scope"] == "report"


class TestFailsClosed:
    async def test_an_inactive_workspace_refuses_rather_than_falling_back(self):
        directory = _FakeDirectory(tenants={"acme": {"is_active": False}})

        with pytest.raises(PermissionError, match="not active"):
            await _runner(directory).user_for("acme", "ada@acme.test")

    async def test_an_unknown_workspace_refuses(self):
        directory = _FakeDirectory(tenants={})

        with pytest.raises(PermissionError):
            await _runner(directory).user_for("does-not-exist", "ada@acme.test")

    async def test_a_departed_member_refuses_rather_than_using_a_stale_identity(self):
        """The scenario the module's docstring names explicitly: removing
        somebody from a workspace must stop their reports on the next tick."""
        directory = _FakeDirectory(
            tenants={"acme": {"is_active": True}}, members={}
        )

        with pytest.raises(PermissionError, match="no longer a member"):
            await _runner(directory).user_for("acme", "departed@acme.test")

    async def test_a_disabled_member_refuses(self):
        directory = _FakeDirectory(
            tenants={"acme": {"is_active": True}},
            members={
                ("acme", "ada@acme.test"): {
                    "role": "viewer",
                    "is_active": False,
                    "full_name": "Ada",
                }
            },
        )

        with pytest.raises(PermissionError, match="disabled"):
            await _runner(directory).user_for("acme", "ada@acme.test")

    async def test_no_fallback_identity_no_exception_swallowed(self):
        """There is deliberately no default/anonymous identity a failed
        resolution falls back to -- the only two outcomes are a resolved User
        or a raised PermissionError."""
        directory = _FakeDirectory(tenants={"acme": {"is_active": True}}, members={})

        try:
            await _runner(directory).user_for("acme", "nobody@acme.test")
        except PermissionError:
            pass
        else:
            pytest.fail("expected user_for to raise, not return a fallback identity")
