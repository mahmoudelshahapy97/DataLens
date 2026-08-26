"""Who a report may be sent to, and where a webhook may point.

Both are the same class of bug wearing different clothes. A report renders with
one member's permissions; the recipient list decides who those permissions get
shared with, and the webhook URL decides which host inside the deployment's own
network the API container can be made to talk to by editing a text field.

Neither needs a database, a mail server or the network: `permitted_recipients`
takes a directory it only calls one method on, and `check_webhook_url` refuses
before it would connect.
"""

from __future__ import annotations

import pytest

from vanna_app.report_delivery import (
    DeliveryRefused,
    check_webhook_url,
    permitted_recipients,
)


class FakeDirectory:
    """Just enough of ``Directory`` for the recipient check."""

    def __init__(self, members):
        self._members = members

    async def list_members(self, tenant_id):  # noqa: ARG002 - one tenant in these tests
        return self._members


MEMBERS = [
    {"email": "ada@example.com", "is_active": True},
    {"email": "Grace@Example.com", "is_active": True},
    {"email": "gone@example.com", "is_active": False},
]


# ----------------------------------------------------------------------
# Recipients
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_members_receive_by_default():
    permitted = await permitted_recipients(
        FakeDirectory(MEMBERS),
        "acme",
        ["ada@example.com", "outsider@elsewhere.com"],
        allow_external=False,
    )
    assert permitted == ["ada@example.com"]


@pytest.mark.asyncio
async def test_membership_is_case_insensitive():
    """Addresses are compared casefolded on both sides.

    `Grace@Example.com` in the directory and `grace@example.com` on the schedule
    are the same person, and a case-sensitive comparison silently drops them --
    a recipient who stops receiving reports with no error anywhere.
    """
    permitted = await permitted_recipients(
        FakeDirectory(MEMBERS), "acme", ["grace@example.com"], allow_external=False
    )
    assert permitted == ["grace@example.com"]


@pytest.mark.asyncio
async def test_a_disabled_member_is_dropped():
    """Access disabled this morning means no report this evening.

    Membership is read at delivery time precisely so this holds without anybody
    having to remember to edit the schedule.
    """
    permitted = await permitted_recipients(
        FakeDirectory(MEMBERS), "acme", ["gone@example.com"], allow_external=False
    )
    assert permitted == []


@pytest.mark.asyncio
async def test_one_bad_address_does_not_lose_the_others():
    """A partial list still goes.

    Failing the whole delivery because one recipient left means nobody gets it.
    """
    permitted = await permitted_recipients(
        FakeDirectory(MEMBERS),
        "acme",
        ["ada@example.com", "gone@example.com", "outsider@elsewhere.com"],
        allow_external=False,
    )
    assert permitted == ["ada@example.com"]


@pytest.mark.asyncio
async def test_external_recipients_are_opt_in():
    permitted = await permitted_recipients(
        FakeDirectory(MEMBERS),
        "acme",
        ["outsider@elsewhere.com"],
        allow_external=True,
    )
    assert permitted == ["outsider@elsewhere.com"]


# ----------------------------------------------------------------------
# Webhooks
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",          # not https
        "https://user:pass@example.com/x",  # credentials in the URL
        "https:///nohost",                  # no host
        "ftp://example.com/hook",           # not https
    ],
)
def test_refuses_a_malformed_or_insecure_url(url: str) -> None:
    with pytest.raises(DeliveryRefused):
        check_webhook_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/hook",
        "https://localhost/hook",
        "https://169.254.169.254/latest/meta-data/",   # cloud metadata
        "https://10.0.0.5/hook",
        "https://192.168.1.10/hook",
        "https://172.16.4.4/hook",
        "https://[::1]/hook",
    ],
)
def test_refuses_the_deployments_own_network(url: str) -> None:
    """The whole point of the guard.

    `169.254.169.254` is the one that matters: it is a request to the cloud
    metadata service, made by the API container with its own credentials, caused
    by somebody typing a URL into an admin form.
    """
    with pytest.raises(DeliveryRefused):
        check_webhook_url(url)


def test_allow_list_wins_over_dns() -> None:
    """A configured allow-list is checked instead of resolving.

    Deliberate: DNS resolution here has a rebinding gap (the name can resolve
    differently when the request is actually made), so a deployment that knows its
    endpoints should pin them and not rely on the resolver at all.
    """
    assert check_webhook_url(
        "https://hooks.slack.com/services/T/B/X",
        allowed_hosts=["slack.com"],
    )


def test_allow_list_is_not_a_suffix_match() -> None:
    """`evil-slack.com` must not pass an allow-list containing `slack.com`.

    A bare `endswith` is the natural way to write "or a subdomain of" and it is
    wrong in exactly this way.
    """
    with pytest.raises(DeliveryRefused):
        check_webhook_url("https://evil-slack.com/hook", allowed_hosts=["slack.com"])


def test_allow_list_permits_a_real_subdomain() -> None:
    assert check_webhook_url("https://hooks.slack.com/x", allowed_hosts=["slack.com"])


def test_allow_list_permits_an_exact_host() -> None:
    assert check_webhook_url("https://slack.com/x", allowed_hosts=["slack.com"])
