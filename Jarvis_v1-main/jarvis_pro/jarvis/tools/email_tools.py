"""Email over IMAP/SMTP (Gmail, Outlook, Zoho, any standard provider).

.env: EMAIL_ADDRESS, EMAIL_APP_PASSWORD, IMAP_HOST, SMTP_HOST, SMTP_PORT
"""

from __future__ import annotations

import asyncio
import email
import imaplib
import smtplib
import time
from email.header import decode_header, make_header
from email.message import EmailMessage, Message

from livekit.agents import RunContext, ToolError, function_tool

from .. import config
from ..guard import action
from .memory_tools import find_contact


# ---------------------------------------------------------------- helpers ----
def creds() -> dict[str, str]:
    c = config.require_env("EMAIL_ADDRESS", "EMAIL_APP_PASSWORD")
    c["IMAP_HOST"] = config.env("IMAP_HOST", "imap.gmail.com")
    c["SMTP_HOST"] = config.env("SMTP_HOST", "smtp.gmail.com")
    c["SMTP_PORT"] = config.env("SMTP_PORT", "465")
    return c


def decode(value: str | None) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except Exception:  # noqa: BLE001
        return value or ""


def text_body(msg: Message) -> str:
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
            payload = part.get_payload(decode=True) or b""
            return payload.decode(part.get_content_charset() or "utf-8", "replace")
    return ""


def build_message(to: str, subject: str, body: str, in_reply_to: str | None = None) -> EmailMessage:
    c = creds()
    msg = EmailMessage()
    msg["From"] = c["EMAIL_ADDRESS"]
    msg["To"] = to
    msg["Subject"] = subject
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg["Auto-Submitted"] = "auto-generated" if in_reply_to else "no"
    msg.set_content(body)
    return msg


def smtp_send(msg: EmailMessage) -> None:
    c = creds()
    port = int(c["SMTP_PORT"])
    if port == 465:
        with smtplib.SMTP_SSL(c["SMTP_HOST"], port, timeout=30) as s:
            s.login(c["EMAIL_ADDRESS"], c["EMAIL_APP_PASSWORD"])
            s.send_message(msg)
    else:  # 587 STARTTLS (Outlook / Office 365)
        with smtplib.SMTP(c["SMTP_HOST"], port, timeout=30) as s:
            s.starttls()
            s.login(c["EMAIL_ADDRESS"], c["EMAIL_APP_PASSWORD"])
            s.send_message(msg)


def imap_connect() -> imaplib.IMAP4_SSL:
    c = creds()
    imap = imaplib.IMAP4_SSL(c["IMAP_HOST"])
    imap.login(c["EMAIL_ADDRESS"], c["EMAIL_APP_PASSWORD"])
    return imap


def save_draft(msg: EmailMessage) -> None:
    c = creds()
    folder = '"[Gmail]/Drafts"' if "gmail" in c["IMAP_HOST"] else "Drafts"
    with imap_connect() as imap:
        imap.append(folder, r"\Draft", imaplib.Time2Internaldate(time.time()), msg.as_bytes())


def fetch_emails(count: int = 5, unread_only: bool = True, from_sender: str = "") -> list[dict]:
    with imap_connect() as imap:
        imap.select("INBOX", readonly=True)
        criteria = ["UNSEEN" if unread_only else "ALL"]
        if from_sender:
            criteria += ["FROM", f'"{from_sender}"']
        _, data = imap.search(None, *criteria)
        ids = data[0].split()[-count:]
        out = []
        for i in reversed(ids):
            _, msg_data = imap.fetch(i, "(BODY.PEEK[])")
            msg = email.message_from_bytes(msg_data[0][1])
            out.append({
                "from": decode(msg["From"]),
                "subject": decode(msg["Subject"]),
                "date": msg["Date"],
                "body": " ".join(text_body(msg).split())[:1200],
            })
        return out


def resolve_address(to: str) -> str:
    if "@" in to:
        return to.strip()
    c = find_contact(to)
    if c and c.get("email"):
        return c["email"]
    raise ToolError(f"I don't have an email address for '{to}'. Ask Boss for the full address.")


# ------------------------------------------------------------------ tools ----
@function_tool
async def check_email(context: RunContext, count: int = 5, unread_only: bool = True, from_sender: str = "") -> str:
    """Read Boss's latest emails. Summarise them; don't read whole bodies aloud unless asked.

    Args:
        count: How many emails (1-20).
        unread_only: Only unread emails.
        from_sender: Optional sender name or address to filter by.
    """
    count = max(1, min(count, 20))
    async with action(context, "email.read", f"Read {count} {'unread ' if unread_only else ''}emails"
                      + (f" from {from_sender}" if from_sender else "")):
        mails = await asyncio.to_thread(fetch_emails, count, unread_only, from_sender)
    if not mails:
        return "No matching emails."
    return "\n\n".join(f"From: {m['from']}\nSubject: {m['subject']}\nDate: {m['date']}\n{m['body'][:500]}"
                       for m in mails)


@function_tool
async def send_email(context: RunContext, to: str, subject: str, body: str, confirmed: bool = False) -> str:
    """Send an email from Boss's account.

    Args:
        to: Email address, or a contact name from contacts.
        subject: Subject line.
        body: Full plain-text body, signed appropriately.
        confirmed: Set true ONLY after Boss verbally confirmed (when asked to).
    """
    address = resolve_address(to)
    async with action(context, "email.send", f"Send email to {address}: \"{subject}\"",
                      {"to": address, "subject": subject, "body": body}, confirmed):
        await asyncio.to_thread(smtp_send, build_message(address, subject, body))
    return f"Email sent to {address}."


@function_tool
async def draft_email(context: RunContext, to: str, subject: str, body: str) -> str:
    """Save an email as a draft in Boss's mailbox (not sent).

    Args:
        to: Email address, or a contact name from contacts.
        subject: Subject line.
        body: Full plain-text body.
    """
    address = resolve_address(to)
    async with action(context, "email.draft", f"Draft email to {address}: \"{subject}\"",
                      {"to": address, "subject": subject, "body": body}):
        await asyncio.to_thread(save_draft, build_message(address, subject, body))
    return f"Draft saved for {address}."


TOOLS = [
    ("email.read", check_email),
    ("email.send", send_email),
    ("email.draft", draft_email),
]
