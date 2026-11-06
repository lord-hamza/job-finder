import os
import smtplib
import socket
import ssl
from unittest import mock

from django.core import mail

from core import mailer
from core.tests.base import AppTestCase


def send(**overrides):
    args = {
        "to": "careers@acme.co.uk",
        "subject": "Application",
        "body": "Hello",
        "from_name": "Sam Taylor",
        "attachment_name": "Sam_Taylor_CV.pdf",
        "attachment": b"%PDF cv",
        "mimetype": "application/pdf",
    }
    args.update(overrides)
    mailer.send(**args)


class SendTests(AppTestCase):
    def test_message_contents(self):
        send()
        message = mail.outbox[0]
        built = message.message()
        self.assertEqual(message.to, ["careers@acme.co.uk"])
        self.assertEqual(built["From"], "Sam Taylor <sam@gmail.com>")
        self.assertIn("@gmail.com>", built["Message-ID"])
        self.assertEqual(message.attachments[0][:3], ("Sam_Taylor_CV.pdf", b"%PDF cv", "application/pdf"))

    def test_new_lines_cant_inject_headers(self):
        send(subject="Hi\r\nBcc: evil@evil.com", from_name="Sam\nBcc: evil@evil.com")
        built = mail.outbox[0].message()
        self.assertIsNone(built["Bcc"])
        self.assertEqual(built["Subject"], "Hi Bcc: evil@evil.com")

    def test_non_ascii_name_and_subject(self):
        send(from_name="Zoë Müller", subject="Bewerbung für Entwickler")
        built = mail.outbox[0].message().as_bytes()
        self.assertIn(b"sam@gmail.com", built)

    def test_app_password_spaces_removed(self):
        self.assertEqual(mailer.password(), "abcdefghijklmnop")

    def test_uses_gmail_ssl_port(self):
        with mock.patch("core.mailer.get_connection") as get_connection:
            get_connection.return_value.send_messages.return_value = 1
            send()
        kwargs = get_connection.call_args.kwargs
        self.assertEqual((kwargs["host"], kwargs["port"], kwargs["use_ssl"]), ("smtp.gmail.com", 465, True))
        self.assertEqual((kwargs["username"], kwargs["password"]), ("sam@gmail.com", "abcdefghijklmnop"))

    def test_not_configured(self):
        with mock.patch.dict(os.environ, {"GMAIL_APP_PASSWORD": ""}):
            self.assertFalse(mailer.is_configured())
            with self.assertRaisesMessage(mailer.MailError, "isn't connected"):
                send()

    def test_errors_become_readable_messages(self):
        cases = [
            (smtplib.SMTPAuthenticationError(535, b"BadCredentials"), "App Password"),
            (smtplib.SMTPRecipientsRefused({"x": (550, b"no")}), "refused that recipient"),
            (smtplib.SMTPDataError(550, b"5.4.5 Daily limit exceeded"), "Daily limit exceeded"),
            (smtplib.SMTPSenderRefused(553, b"not allowed", "x"), "refused the message"),
            (socket.timeout("timed out"), "Couldn't reach"),
            (socket.gaierror("no dns"), "Couldn't reach"),
            (ConnectionRefusedError("refused"), "Couldn't reach"),
            (ssl.SSLError("bad cert"), "Couldn't reach"),
            (smtplib.SMTPServerDisconnected("gone"), "Email failed"),
        ]
        for error, expected in cases:
            with self.subTest(type(error).__name__):
                with mock.patch("core.mailer.get_connection") as get_connection:
                    get_connection.return_value.send_messages.side_effect = error
                    with self.assertRaisesMessage(mailer.MailError, expected):
                        send()

    def test_password_never_appears_in_errors(self):
        with mock.patch("core.mailer.get_connection") as get_connection:
            get_connection.return_value.send_messages.side_effect = smtplib.SMTPAuthenticationError(535, b"no")
            with self.assertRaises(mailer.MailError) as caught:
                send()
        self.assertNotIn("abcdefghijklmnop", str(caught.exception))

    def test_nothing_delivered_is_an_error(self):
        with mock.patch("core.mailer.get_connection") as get_connection:
            get_connection.return_value.send_messages.return_value = 0
            with self.assertRaisesMessage(mailer.MailError, "didn't accept"):
                send()


class VerifyTests(AppTestCase):
    def test_good_login(self):
        with mock.patch("core.mailer.get_connection") as get_connection:
            mailer.verify("sam@gmail.com", "pw")
        get_connection.return_value.open.assert_called_once()
        get_connection.return_value.close.assert_called_once()

    def test_bad_login(self):
        with mock.patch("core.mailer.get_connection") as get_connection:
            get_connection.return_value.open.side_effect = smtplib.SMTPAuthenticationError(535, b"no")
            with self.assertRaisesMessage(mailer.MailError, "App Password"):
                mailer.verify("sam@gmail.com", "pw")
