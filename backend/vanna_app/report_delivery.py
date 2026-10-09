"""Getting a rendered report to the people it is for.

Four channels, and the interesting part of all four is *who is allowed to receive*.

A report renders with one member's permissions. If the recipient list were free
text, a viewer with a schedule could mail their own restricted view to anybody --
or, worse, an admin could schedule their own unrestricted view to an address
outside the company and nothing in the system would object. A schedule that can
mail arbitrary addresses is a grant bypass with a mail server attached.

So: **recipients must be members of the workspace**, unless the deployment has
explicitly opted into external addresses. That is one setting, it is off by
default, and it is checked at delivery time rather than only at write time --
because membership changes and a schedule outlives it.

The webhook channel carries no data at all, only a notification that a report ran
and a link to fetch it. A webhook URL is configured by a workspace admin and
points anywhere on the network the API container can reach, so treating it as a
place to put rows would be the same mistake in a different shape.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from typing import Any, Dict, List, Optional, Sequence, Set
from urllib.parse import urlparse

from .mail import Attachment, Message

logger = logging.getLogger("vanna.reports.delivery")

#: Attachments above this are not sent by mail. A full HTML export is ~1.3MB, so
#: this is generous -- it exists to stop a thousand-row table becoming a message
#: that every mail server in the path rejects, after the run has already been
#: marked delivered.
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


class DeliveryRefused(Exception):
    """A recipient or destination this deployment will not send to."""


# ----------------------------------------------------------------------
# Who may receive
# ----------------------------------------------------------------------


async def permitted_recipients(
    directory: Any,
    tenant_id: str,
    requested: Sequence[str],
    *,
    allow_external: bool,
) -> List[str]:
    """The subset of ``requested`` this workspace may actually mail.

    Membership is read at delivery time, not trusted from when the schedule was
    written. Somebody removed from the workspace this morning does not receive
    this evening's report.

    An address that is refused is *dropped and logged*, not fatal: a schedule with
    four recipients where one has left should still reach the other three, and
    failing the whole run would mean nobody gets it because of somebody who is
    gone.
    """
    wanted = {address.strip().lower() for address in requested if address and address.strip()}
    if not wanted:
        return []

    if allow_external:
        return sorted(wanted)

    members: Set[str] = {
        (row.get("email") or "").lower()
        for row in (await directory.list_members(tenant_id) or [])
        if row.get("is_active", True)
    }

    permitted = sorted(wanted & members)
    refused = sorted(wanted - members)
    if refused:
        logger.warning(
            "Not sending the report for %s to %d non-member address(es): %s. "
            "Set VANNA_REPORT_ALLOW_EXTERNAL_RECIPIENTS=true to permit them.",
            tenant_id, len(refused), ", ".join(refused),
        )
    return permitted


# ----------------------------------------------------------------------
# Webhooks
# ----------------------------------------------------------------------

#: Ranges a webhook must never resolve into. A URL is supplied by an administrator
#: and fetched *by the API container*, which sits inside the network -- so
#: `http://169.254.169.254/` is a request for the cloud metadata service, made
#: with the container's own credentials, triggered by editing a text field.
#:
#: Note that `net.py` does not cover this: that module works out which client an
#: inbound request came from. This is the outbound direction and needed its own
#: guard.
_FORBIDDEN_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local, incl. cloud metadata
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),    # carrier-grade NAT
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),         # unique local
    ipaddress.ip_network("fe80::/10"),        # link-local
)


def check_webhook_url(url: str, *, allowed_hosts: Sequence[str] = ()) -> str:
    """Refuse a webhook destination that should not be reachable.

    Four checks, in order of how cheaply they refuse:

    1. **https only.** A report notification over plain http is readable by every
       hop, and the link it carries is a link to data.
    2. **Host allow-list**, when one is configured. The strongest control
       available and the one to prefer in a deployment that knows its endpoints.
    3. **No credentials in the URL.** ``https://user:pass@host`` puts a secret in
       a database column and in every log line that echoes the destination.
    4. **Resolved address is public.** DNS is resolved *here* and every returned
       address is checked, because a hostname that looks external can resolve to
       169.254.169.254.

    Point 4 is not airtight on its own -- the name could resolve differently when
    the request is actually made, which is the DNS-rebinding gap -- so it is a
    layer under the allow-list rather than a replacement for it.
    """
    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise DeliveryRefused("A webhook URL must use https.")
    if not parsed.hostname:
        raise DeliveryRefused("That webhook URL has no host.")
    if parsed.username or parsed.password:
        raise DeliveryRefused(
            "Credentials in a webhook URL are stored in the clear. Use a secret "
            "path or a token header instead."
        )

    host = parsed.hostname.lower()

    if allowed_hosts:
        permitted = {h.strip().lower() for h in allowed_hosts if h.strip()}
        # An exact match or a subdomain of an allowed host. Not `endswith` alone:
        # that lets `evil-example.com` past an allow-list containing `example.com`.
        if not any(host == entry or host.endswith("." + entry) for entry in permitted):
            raise DeliveryRefused(
                f"{host} is not in VANNA_WEBHOOK_ALLOWED_HOSTS."
            )
        return url

    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise DeliveryRefused(f"Could not resolve {host}.") from exc

    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        for network in _FORBIDDEN_NETWORKS:
            if address.version == network.version and address in network:
                raise DeliveryRefused(
                    f"{host} resolves to {address}, which is inside the "
                    "deployment's own network. Webhooks may only reach public "
                    "addresses."
                )
    return url


def _card(run: Dict[str, Any], dashboard: Any, results: List[Any], link: str) -> Dict[str, Any]:
    """The webhook payload: that it ran, not what it said.

    Slack and Teams both render this shape adequately without their own builders --
    `text` is what a notification actually needs, and a Block Kit / Adaptive Card
    pair would be two formats to keep correct for no gain in what is conveyed.
    """
    failed = [r for r in results if r.error]
    rows = sum(r.row_count for r in results)
    title = dashboard.title or "Report"

    lines = [
        f"*{title}* ran successfully.",
        f"{len(results)} tile(s), {rows:,} row(s).",
    ]
    if failed:
        lines.append(f"{len(failed)} tile(s) failed.")
    if link:
        lines.append(f"Open it: {link}")
    lines.append(
        "This message carries no data. Whoever opens the link sees it with their "
        "own permissions."
    )

    return {"text": "\n".join(lines)}


# ----------------------------------------------------------------------
# Delivery
# ----------------------------------------------------------------------


class Delivery:
    """Sends a finished run down every channel it names.

    A channel that fails is logged and the others still go. A run delivered to
    three of four destinations is a partial success worth recording; failing the
    whole run would re-deliver to the three that already got it on the next tick.
    """

    def __init__(
        self,
        *,
        mailer: Any,
        store: Any,
        directory: Any,
        settings: Any,
    ) -> None:
        self.mailer = mailer
        self.store = store
        self.directory = directory
        self.settings = settings

    @property
    def _allow_external(self) -> bool:
        return bool(getattr(self.settings, "report_allow_external_recipients", False))

    @property
    def _allowed_hosts(self) -> Sequence[str]:
        return getattr(self.settings, "webhook_allowed_hosts", ()) or ()

    @property
    def _base_url(self) -> str:
        return (getattr(self.settings, "public_base_url", "") or "").rstrip("/")

    async def __call__(
        self,
        *,
        run: Dict[str, Any],
        dashboard: Any,
        results: List[Any],
        user: Any,
        tenant: Any,
        artifact: bytes,
        filename: str,
    ) -> None:
        link = f"{self._base_url}/reports/{run.get('schedule_id') or ''}" if self._base_url else ""

        for channel in run.get("channels") or []:
            kind = (channel or {}).get("kind")
            target = (channel or {}).get("target") or ""
            try:
                if kind == "email":
                    await self._email(run, dashboard, results, tenant, artifact, filename, target)
                elif kind == "webhook":
                    await self._webhook(run, dashboard, results, target, link)
                elif kind == "inapp":
                    await self._inapp(run, dashboard, results, target or user.email)
                else:
                    logger.warning("Unknown report channel %r; skipped", kind)
            except Exception as exc:  # noqa: BLE001 - one channel must not stop the rest
                logger.warning(
                    "Report %s: %s delivery failed: %s: %s",
                    run["id"], kind, type(exc).__name__, exc,
                )

    async def _email(
        self,
        run: Dict[str, Any],
        dashboard: Any,
        results: List[Any],
        tenant: Any,
        artifact: bytes,
        filename: str,
        target: str,
    ) -> None:
        recipients = await permitted_recipients(
            self.directory,
            run["tenant_id"],
            [address for address in target.replace(";", ",").split(",")],
            allow_external=self._allow_external,
        )
        if not recipients:
            logger.warning(
                "Report %s: no permitted recipients; nothing sent.", run["id"]
            )
            return

        workspace = (tenant or {}).get("name") or run["tenant_id"]
        title = dashboard.title or "Report"
        rows = sum(r.row_count for r in results)
        oversized = len(artifact) > MAX_ATTACHMENT_BYTES

        text = (
            f"{title}\n"
            f"{workspace}\n\n"
            f"{len(results)} tile(s), {rows:,} row(s).\n\n"
            + (
                "The attachment was too large to send; open the report in DataLens "
                "to download it.\n"
                if oversized
                else "The attached file opens in a browser with no login and no "
                     "network connection.\n"
            )
            + f"\nThese figures are the ones {run['run_as']} is permitted to see.\n"
        )

        attachments = ()
        if not oversized:
            attachments = (
                Attachment(
                    filename=filename,
                    content=artifact,
                    # The two formats this produces. Naming them rather than
                    # sending everything as octet-stream is what makes a mail
                    # client offer "open in browser" for the HTML export.
                    maintype="text" if filename.endswith(".html") else "application",
                    subtype="html" if filename.endswith(".html") else "zip",
                ),
            )

        message = Message(
            to=", ".join(recipients),
            subject=f"{title} - {workspace}",
            text=text,
            html=_email_html(title, workspace, len(results), rows, run["run_as"], oversized),
            attachments=attachments,
        )

        await self.mailer.send(message)
        logger.info("Report %s mailed to %d recipient(s)", run["id"], len(recipients))

    async def _webhook(
        self, run: Dict[str, Any], dashboard: Any, results: List[Any], target: str, link: str
    ) -> None:
        import httpx

        url = check_webhook_url(target, allowed_hosts=self._allowed_hosts)
        payload = _card(run, dashboard, results, link)

        async with httpx.AsyncClient(
            timeout=10.0,
            # A redirect is how an allowed host hands the request to a forbidden
            # one; the check above would have already passed by then.
            follow_redirects=False,
        ) as client:
            response = await client.post(url, json=payload)
            if response.status_code >= 400:
                raise DeliveryRefused(
                    f"Webhook returned {response.status_code}: {response.text[:200]}"
                )

    async def _inapp(
        self, run: Dict[str, Any], dashboard: Any, results: List[Any], target: str
    ) -> None:
        rows = sum(r.row_count for r in results)
        await self.store.notify(
            run["tenant_id"],
            target,
            title=f"{dashboard.title or 'Report'} is ready",
            body=f"{len(results)} tile(s), {rows:,} row(s).",
            # A path, never an absolute URL: a stored absolute URL becomes a
            # stored open redirect the moment the deployment's hostname changes.
            link=f"/reports/{run.get('schedule_id') or ''}",
        )


def _email_html(
    title: str, workspace: str, tiles: int, rows: int, run_as: str, oversized: bool
) -> str:
    """A plain, inline-styled message.

    No workspace branding, deliberately. A report that arrives looking like an
    official communication from the recipient's own company is a better phishing
    template than it is a feature, and the value -- knowing which workspace it
    came from -- is carried by naming the workspace in text.
    """
    from html import escape

    note = (
        "The attachment was too large to send. Open the report in DataLens to "
        "download it."
        if oversized
        else "The attached file opens in a browser with no login and no network "
             "connection."
    )
    return (
        '<div style="font:15px/1.55 ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;'
        'color:#0f172a;max-width:560px">'
        f'<h2 style="margin:0 0 4px;font-size:1.15rem">{escape(title)}</h2>'
        f'<p style="margin:0 0 18px;color:#64748b;font-size:.875rem">{escape(workspace)}</p>'
        f'<p style="margin:0 0 12px">{tiles} tile(s), {rows:,} row(s).</p>'
        f'<p style="margin:0 0 12px;color:#64748b;font-size:.875rem">{escape(note)}</p>'
        f'<p style="margin:0;color:#64748b;font-size:.8125rem">These figures are the '
        f'ones {escape(run_as)} is permitted to see. Another member may be '
        'permitted to see more, or less.</p>'
        "</div>"
    )
