"""Sends through Gmail SMTP. Port 465 because many networks block 587.

Several Gmail accounts can be connected. .env keeps them as GMAIL_ADDRESS and GMAIL_APP_PASSWORD, then
GMAIL_ADDRESS_2 and GMAIL_APP_PASSWORD_2, and so on."""
import os
import smtplib
import socket
import ssl
from email.utils import formataddr, make_msgid

from django.core.mail import EmailMessage, get_connection

from .emails import mailbox

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
TIMEOUT = 30
MAX_ACCOUNTS = 10


class MailError(Exception):
    """Message is shown to the user."""


def env_keys(slot):
    """The .env names for one account. The first has no number, as before there could be more than one."""
    suffix = "" if slot == 1 else f"_{slot}"
    return f"GMAIL_ADDRESS{suffix}", f"GMAIL_APP_PASSWORD{suffix}"


def _slots():
    """(slot, address, app password) for each account that has both."""
    for slot in range(1, MAX_ACCOUNTS + 1):
        user_key, secret_key = env_keys(slot)
        user = os.environ.get(user_key, "").strip().lower()
        secret = "".join(os.environ.get(secret_key, "").split())  # Google shows them as "abcd efgh ijkl mnop"
        if user and secret:
            yield slot, user, secret


def accounts():
    """Connected addresses, in the order they were added. The same mailbox twice counts once."""
    found = {}
    for _, user, _ in _slots():
        found.setdefault(mailbox(user), user)
    return list(found.values())


def account(address):
    """The connected address that reaches the same mailbox, such as a.b@gmail.com for ab@gmail.com, or ""."""
    wanted = mailbox(address)
    return next((user for user in accounts() if wanted and mailbox(user) == wanted), "")


def slot_for(address):
    """Where to keep this account in .env: its own slot if it's there already, else the first empty one, else None."""
    wanted, empty = mailbox(address), None
    for slot in range(1, MAX_ACCOUNTS + 1):
        user = os.environ.get(env_keys(slot)[0], "").strip()
        if user and mailbox(user) == wanted:
            return slot
        if not user and empty is None:
            empty = slot
    return empty


def slots_of(address):
    """Every slot holding this mailbox, for disconnecting it."""
    wanted = mailbox(address)
    return [
        slot for slot in range(1, MAX_ACCOUNTS + 1)
        if wanted and mailbox(os.environ.get(env_keys(slot)[0], "")) == wanted
    ]


def address():
    """The first connected account."""
    found = accounts()
    return found[0] if found else ""


def password(user=""):
    wanted = mailbox(user or address())
    return next((secret for _, found, secret in _slots() if mailbox(found) == wanted), "")


def is_configured():
    return bool(accounts())


def _connection(user, secret):
    return get_connection(
        host=SMTP_HOST, port=SMTP_PORT, username=user, password=secret,
        use_ssl=True, timeout=TIMEOUT, fail_silently=False,
    )


def verify(user, secret):
    """Logs in without sending anything. Raises MailError if Gmail says no."""
    connection = _connection(user, secret)
    try:
        connection.open()
    except (OSError, ValueError) as exc:
        raise MailError(_explain(exc)) from exc
    finally:
        connection.close()


def send(to, subject, body, from_name, attachment_name, attachment, mimetype, sender=""):
    """Sends one email with the CV attached, from the connected account sender (the first if not given).
    Raises MailError with a readable message."""
    if not is_configured():
        raise MailError("Gmail isn't connected yet.")
    user = account(sender) if sender else address()
    if not user:
        raise MailError(f"{sender} isn't connected any more. Connect it again in Setup, or choose another account for this CV.")
    message = EmailMessage(
        subject=" ".join(subject.split()),
        body=body,
        from_email=formataddr((" ".join(from_name.split()), user)),
        to=[to],
        connection=_connection(user, password(user)),
        headers={"Message-ID": make_msgid(domain=user.rsplit("@", 1)[-1])},
    )
    message.attach(attachment_name, attachment, mimetype)
    try:
        delivered = message.send(fail_silently=False)
    except (OSError, ValueError) as exc:
        raise MailError(_explain(exc)) from exc
    if not delivered:
        raise MailError("Gmail didn't accept the message.")


def _explain(exc):
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return (
            "Gmail rejected the login. Use a 16-character App Password from "
            "myaccount.google.com/apppasswords, not your normal password."
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "Gmail refused that recipient address."
    if isinstance(exc, (smtplib.SMTPSenderRefused, smtplib.SMTPDataError)):
        detail = exc.smtp_error.decode(errors="replace") if isinstance(exc.smtp_error, bytes) else exc.smtp_error
        return f"Gmail refused the message ({exc.smtp_code}): {str(detail)[:200]}"
    if isinstance(exc, (socket.timeout, TimeoutError, socket.gaierror, ConnectionError, ssl.SSLError)):
        return f"Couldn't reach {SMTP_HOST}:{SMTP_PORT}. Check your internet connection or firewall."
    return f"Email failed: {str(exc)[:200]}"
