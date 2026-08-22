"""Outbound email: password resets and account invitations.

There was no mail at all. A forgotten password needed a platform admin, and creating
an account produced a temporary password with the instruction to "give this to them
over a channel you trust" -- which in practice means a chat message that outlives
the password.

Two backends behind one interface:

``SmtpMailer``     real delivery, over ``aiosmtplib`` so a slow mail server cannot
                   block the event loop that is streaming somebody's answer.
``ConsoleMailer``  logs the message, including the link. The default when no host is
                   configured, so the whole reset flow is exercisable in development
                   and in tests without a mail server -- and so a misconfigured
                   production deployment fails by *not sending* rather than by
                   crashing a request.

Both are best-effort at the call site: a failure to send must not tell the caller
whether the address existed, which is the property the whole reset flow depends on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

logger = logging.getLogger("vanna.mail")


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    text: str
    html: str = ""


class Mailer:
    """How a message leaves the building."""

    async def send(self, message: Message) -> bool:
        raise NotImplementedError


class ConsoleMailer(Mailer):
    """Writes the message to the log instead of sending it."""

    async def send(self, message: Message) -> bool:
        logger.warning(
            "MAIL (not sent -- no SMTP configured)\n"
            "  to:      %s\n  subject: %s\n%s",
            message.to,
            message.subject,
            "\n".join(f"  | {line}" for line in message.text.splitlines()),
        )
        return True


class SmtpMailer(Mailer):
    """Delivers over SMTP."""

    def __init__(
        self,
        *,
        host: str,
        port: int = 587,
        username: str = "",
        password: str = "",
        sender: str = "vanna@localhost",
        starttls: bool = True,
        timeout: int = 15,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.starttls = starttls
        self.timeout = timeout

    async def send(self, message: Message) -> bool:
        from email.message import EmailMessage

        payload = EmailMessage()
        payload["From"] = self.sender
        payload["To"] = message.to
        payload["Subject"] = message.subject
        payload.set_content(message.text)
        if message.html:
            payload.add_alternative(message.html, subtype="html")

        try:
            import aiosmtplib

            await aiosmtplib.send(
                payload,
                hostname=self.host,
                port=self.port,
                username=self.username or None,
                password=self.password or None,
                start_tls=self.starttls,
                timeout=self.timeout,
            )
            logger.info("Sent %r to %s", message.subject, message.to)
            return True
        except ImportError:
            logger.error("aiosmtplib is not installed; cannot send mail.")
            return False
        except Exception as exc:
            # The address is logged, the failure is logged, and the caller is told
            # nothing -- an endpoint that answers differently when delivery fails is
            # an enumeration oracle wearing a different hat.
            logger.error("Could not send %r to %s: %s", message.subject, message.to, exc)
            return False


def build_mailer(settings: Any) -> Mailer:
    if not settings.smtp_host:
        return ConsoleMailer()
    return SmtpMailer(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        sender=settings.smtp_from,
        starttls=settings.smtp_starttls,
    )


# ----------------------------------------------------------------------
# Templates
# ----------------------------------------------------------------------
#
# Plain text first and HTML as an alternative, because a reset link that only
# renders in an HTML client is a reset link some people cannot use. The HTML is
# deliberately trivial: mail clients are a hostile rendering target and there is
# nothing here that needs a layout.


def _shell(body_html: str) -> str:
    return (
        '<div style="font:15px/1.5 -apple-system,Segoe UI,sans-serif;color:#0f172a;'
        'max-width:520px;margin:0 auto;padding:24px">'
        f"{body_html}"
        '<hr style="border:0;border-top:1px solid #e2e8f0;margin:24px 0">'
        '<p style="color:#64748b;font-size:13px">Vanna &middot; if you were not '
        "expecting this message you can ignore it.</p></div>"
    )


def password_reset(*, to: str, token: str, base_url: str, ttl_minutes: int) -> Message:
    link = f"{base_url}/?reset={quote(token)}"
    text = (
        "Someone asked to reset the password for this address.\n\n"
        f"Open this link to choose a new one:\n{link}\n\n"
        f"It works once and expires in {ttl_minutes} minutes.\n\n"
        "If it was not you, nothing has changed and you can ignore this."
    )
    html = _shell(
        "<h2 style='margin:0 0 12px;font-size:19px'>Reset your password</h2>"
        "<p>Someone asked to reset the password for this address.</p>"
        f'<p><a href="{link}" style="display:inline-block;background:#4f46e5;'
        'color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px">'
        "Choose a new password</a></p>"
        f"<p style='color:#64748b;font-size:13px'>The link works once and expires "
        f"in {ttl_minutes} minutes.</p>"
    )
    return Message(to=to, subject="Reset your Vanna password", text=text, html=html)


def account_invitation(
    *, to: str, temporary_password: str, base_url: str, workspace: str, invited_by: str
) -> Message:
    text = (
        f"{invited_by} added you to the {workspace} workspace on Vanna.\n\n"
        f"Sign in at {base_url}\n"
        f"  email:    {to}\n"
        f"  password: {temporary_password}\n\n"
        "You will be asked to choose your own password the first time you sign in."
    )
    html = _shell(
        f"<h2 style='margin:0 0 12px;font-size:19px'>You have been added to "
        f"{workspace}</h2>"
        f"<p>{invited_by} invited you to Vanna.</p>"
        f'<p><a href="{base_url}" style="display:inline-block;background:#4f46e5;'
        'color:#fff;text-decoration:none;padding:10px 18px;border-radius:8px">'
        "Sign in</a></p>"
        f"<p style='font-family:ui-monospace,monospace;background:#f8fafc;"
        f"padding:12px;border-radius:8px'>email: {to}<br>"
        f"password: {temporary_password}</p>"
        "<p style='color:#64748b;font-size:13px'>You will choose your own password "
        "the first time you sign in.</p>"
    )
    return Message(
        to=to, subject=f"You have been added to {workspace} on Vanna", text=text, html=html
    )


def password_changed(*, to: str, base_url: str) -> Message:
    """Notify after a successful change.

    Not a courtesy: it is how the owner of an account finds out that somebody else
    changed their password, which is the only signal they get when the attacker
    controls the session but not the mailbox.
    """
    text = (
        "The password for your Vanna account was just changed, and every other "
        "session was signed out.\n\n"
        "If that was not you, reset it immediately at "
        f"{base_url} and tell your administrator."
    )
    return Message(to=to, subject="Your Vanna password was changed", text=text)
