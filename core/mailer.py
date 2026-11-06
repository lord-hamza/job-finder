"""Sends through Gmail SMTP. Port 465 because many networks block 587."""
import os
import smtplib
import socket
import ssl
from email.utils import formataddr, make_msgid

from django.core.mail import EmailMessage, get_connection

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
TIMEOUT = 30


class MailError(Exception):
    """Message is shown to the user."""


def address():
    return os.environ.get("GMAIL_ADDRESS", "").strip()


def password():
    # Google displays app passwords with spaces: "abcd efgh ijkl mnop".
    return "".join(os.environ.get("GMAIL_APP_PASSWORD", "").split())


def is_configured():
    return bool(address() and password())


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


def send(to, subject, body, from_name, attachment_name, attachment, mimetype):
    """Sends one email with the CV attached. Raises MailError with a readable message."""
    user = address()
    if not is_configured():
        raise MailError("Gmail isn't connected yet.")
    message = EmailMessage(
        subject=" ".join(subject.split()),
        body=body,
        from_email=formataddr((" ".join(from_name.split()), user)),
        to=[to],
        connection=_connection(user, password()),
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
