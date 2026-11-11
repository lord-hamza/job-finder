import os
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core import llm, mailer, pipeline
from core.models import Application, CrawlRun, Profile, Resume
from core.tests.base import CV_TEXT, JD_TEXT, AppTestCase, gemini_job, minimal_pdf

Status = Application.Status
Source = Application.Source
LONG_JD_NO_EMAIL = "Senior Python developer needed for a fully remote role. " * 5


class ViewTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.make_profile()

    def app(self, **fields):
        defaults = {
            "source_hash": os.urandom(16).hex(),
            "source": Source.PASTE,
            "status": Status.READY,
            "title": "Dev",
            "recipient": "careers@acme.co.uk",
            "subject": "Hi",
            "body": "Hello",
            "resume": Resume.objects.first(),
        }
        defaults.update(fields)
        return Application.objects.create(**defaults)


class HomeTests(ViewTestCase):
    def test_renders_when_set_up(self):
        response = self.client.get(reverse("home"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Paste a job description")
        self.assertContains(response, "1 Gmail account")
        self.assertEqual(response.context["missing"], [])

    def test_keys_already_connected_arent_asked_for_again(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, "Add another Gmail account")  # more accounts can always be added
        self.assertNotContains(response, '<details id="add-gmail" class="add-account" open>')
        self.assertNotContains(response, 'name="key"')
        self.assertContains(response, "Gemini connected")

    def test_missing_keys_are_asked_for(self):
        with mock.patch.dict(os.environ, {"GMAIL_APP_PASSWORD": "", "GEMINI_API_KEY": ""}):
            response = self.client.get(reverse("home"))
        self.assertContains(response, 'name="password"')
        self.assertContains(response, 'name="key"')

    def test_setup_stays_open_until_every_cv_has_facts(self):
        self.assertTrue(self.client.get(reverse("home")).context["setup_open"])
        self.client.post(reverse("save_cv", args=[self.cv.pk]), {"full_name": "Sam Taylor", "facts": "Where I live now: Lahore"})
        self.assertFalse(self.client.get(reverse("home")).context["setup_open"])
        Resume.objects.create(name="second.pdf", data=b"%PDF", text="x", full_name="Sam Taylor")
        self.assertTrue(self.client.get(reverse("home")).context["setup_open"])

    def test_empty_install_points_to_setup(self):
        Profile.objects.all().delete()
        Resume.objects.all().delete()
        with mock.patch.dict(os.environ, {"GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": "", "GEMINI_API_KEY": ""}):
            response = self.client.get(reverse("home"))
        self.assertEqual(
            response.context["missing"], ["upload your CV", "connect Gmail", "add your Gemini API key"]
        )
        self.assertContains(response, "CV missing")

    def test_secrets_never_rendered(self):
        response = self.client.get(reverse("home"))
        self.assertNotContains(response, "test-gemini-key")
        self.assertNotContains(response, "abcd efgh")
        self.assertNotContains(response, "abcdefghijklmnop")

    def test_forms_have_no_javascript(self):
        self.assertNotContains(self.client.get(reverse("home")), "<script")

    def test_page_cant_be_framed_but_activity_can(self):
        self.assertEqual(self.client.get(reverse("home"))["X-Frame-Options"], "DENY")
        self.assertEqual(self.client.get(reverse("activity"))["X-Frame-Options"], "SAMEORIGIN")

    def test_post_routes_refuse_get(self):
        app = self.app()
        for url in (
            reverse("upload_resume"), reverse("save_profile"), reverse("save_cv", args=[self.cv.pk]),
            reverse("connect_gmail"), reverse("save_gemini"),
            reverse("apply_jd"), reverse("start_crawl"), reverse("send", args=[app.pk]), reverse("retry", args=[app.pk]),
        ):
            with self.subTest(url):
                self.assertEqual(self.client.get(url).status_code, 405)

    def test_csrf_enforced(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.post(reverse("apply_jd"), {"jd": JD_TEXT}).status_code, 403)
        self.assertEqual(Application.objects.count(), 0)

    def test_unknown_host_refused(self):
        self.assertEqual(self.client.get(reverse("home"), HTTP_HOST="evil.example").status_code, 400)


class ActivityTests(ViewTestCase):
    def test_empty(self):
        self.assertContains(self.client.get(reverse("activity")), "Nothing here yet")

    def test_refreshes_only_while_working(self):
        self.assertNotContains(self.client.get(reverse("activity")), "http-equiv")
        for status in (Status.QUEUED, Status.PROCESSING, Status.SENDING):
            with self.subTest(status):
                app = self.app(status=status)
                self.assertContains(self.client.get(reverse("activity")), 'http-equiv="refresh"')
                app.delete()
        run = CrawlRun.objects.create(url="https://x.example")
        self.assertContains(self.client.get(reverse("activity")), 'http-equiv="refresh"')
        run.delete()
        self.app(status=Status.SENT)
        self.assertNotContains(self.client.get(reverse("activity")), "http-equiv")

    def test_links_escape_the_iframe(self):
        app = self.app()
        self.assertContains(self.client.get(reverse("activity")), f'href="{reverse("detail", args=[app.pk])}" target="_top"')

    def test_web_content_is_escaped(self):
        self.app(title="<script>alert(1)</script>", note="<img src=x onerror=alert(1)>")
        response = self.client.get(reverse("activity"))
        self.assertNotContains(response, "<script>alert")
        self.assertNotContains(response, "<img src=x")

    def counts(self, response):
        return {key: count for key, _, count in response.context["tabs"]}

    def test_counts(self):
        self.app(status=Status.READY)
        self.app(status=Status.MANUAL)
        self.app(status=Status.NEEDS_YOU)
        self.app(status=Status.SENT, sent_at=timezone.now() - timedelta(days=2))
        self.app(status=Status.SENT, sent_at=timezone.now())
        self.app(status=Status.FAILED)
        counts = self.counts(self.client.get(reverse("activity")))
        self.assertEqual(counts, {"all": 6, "today": 1, "sent": 2, "ready": 1, "you": 2, "skipped": 0, "failed": 1})

    def test_tabs_filter_the_list(self):
        ready = self.app(status=Status.READY, title="Ready one")
        failed = self.app(status=Status.FAILED, title="Failed one")
        old = self.app(status=Status.SENT, title="Sent long ago", sent_at=timezone.now() - timedelta(days=2))
        new = self.app(status=Status.SENT, title="Sent just now", sent_at=timezone.now())
        expected = {"all": {ready, failed, old, new}, "ready": {ready}, "failed": {failed}, "sent": {old, new},
                    "today": {new}, "you": set(), "nonsense": {ready, failed, old, new}}
        for show, apps in expected.items():
            with self.subTest(show):
                response = self.client.get(reverse("activity"), {"show": show})
                self.assertEqual(set(response.context["applications"]), apps)
        response = self.client.get(reverse("activity"), {"show": "failed"})
        self.assertContains(response, '<a href="?show=failed" class="tab current" aria-current="page">')
        self.assertContains(response, '<a href="?show=ready" class="tab">')
        self.assertContains(self.client.get(reverse("activity"), {"show": "you"}), "Nothing here.")

    def test_a_filtered_tab_still_refreshes_while_work_goes_on(self):
        self.app(status=Status.PROCESSING)
        self.assertContains(self.client.get(reverse("activity"), {"show": "failed"}), 'http-equiv="refresh"')

    def test_crawl_runs_show_on_all_only(self):
        CrawlRun.objects.create(url="https://x.example/jobs", status=CrawlRun.Status.DONE)
        self.assertContains(self.client.get(reverse("activity")), "https://x.example/jobs")
        self.assertNotContains(self.client.get(reverse("activity"), {"show": "sent"}), "https://x.example/jobs")


class UploadTests(AppTestCase):
    def upload(self, name, data):
        return self.client.post(reverse("upload_resume"), {"resume": SimpleUploadedFile(name, data)}, follow=True)

    def test_good_upload(self):
        details = {"full_name": "Sam Taylor", "phone": "", "linkedin": ""}
        with mock.patch("core.pipeline.resume.extract_text", return_value=CV_TEXT):
            with mock.patch("core.llm.generate_json", return_value=details):
                response = self.upload("cv.pdf", minimal_pdf("x"))
        self.assertContains(response, "CV saved")
        self.assertTrue(Resume.objects.filter(name="cv.pdf").exists())

    def test_no_file(self):
        self.assertContains(self.client.post(reverse("upload_resume"), follow=True), "Choose a PDF")

    def test_wrong_type(self):
        self.assertContains(self.upload("cv.exe", b"MZ"), "PDF or a Word")

    def test_oversized(self):
        self.assertContains(self.upload("cv.pdf", b"%PDF" + b"0" * (10 * 1024 * 1024)), "10 MB")

    def test_garbage_pdf(self):
        response = self.upload("cv.pdf", b"garbage")
        self.assertContains(response, "valid PDF")
        self.assertEqual(Resume.objects.filter(name="cv.pdf").count(), 0)


class ProfileTests(ViewTestCase):
    def test_saves_consent(self):
        self.client.post(reverse("save_profile"), {"allow_consent": "on"})
        self.assertTrue(Profile.load().allow_consent)
        self.client.post(reverse("save_profile"), {})
        self.assertFalse(Profile.load().allow_consent)


class CvDetailsTests(ViewTestCase):
    def save(self, cv, **data):
        data.setdefault("full_name", "Sam Taylor")
        return self.client.post(reverse("save_cv", args=[cv.pk]), data, follow=True)

    def test_name_required(self):
        response = self.save(self.cv, full_name="   ", phone="1")
        self.assertContains(response, "the name is needed to sign the emails that go out with it, so the old one stays")
        self.cv.refresh_from_db()
        self.assertEqual((self.cv.full_name, self.cv.phone), ("Sam Taylor", "1"))

    def test_saves_and_cleans_that_cv_only(self):
        other = Resume.objects.create(name="pk.pdf", data=b"%PDF", text="x", phone="+92 300 1234567")
        response = self.save(
            self.cv,
            full_name="  Sam\r\nBcc: x ",
            phone="  +44 7700\r\n900123 ",
            linkedin=" linkedin.com/in/sam ",
            facts="Where I live now: Leeds\r\nSalary: 50k\r\n",
        )
        self.assertContains(response, "Saved the details for my cv final (2).pdf")
        self.cv.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(
            (self.cv.full_name, self.cv.phone, self.cv.linkedin, self.cv.facts),
            ("Sam Bcc: x", "+44 7700 900123", "linkedin.com/in/sam", "Where I live now: Leeds\nSalary: 50k"),
        )
        self.assertEqual((other.full_name, other.phone, other.linkedin, other.facts), ("", "+92 300 1234567", "", ""))

    def test_unsent_emails_take_the_new_details(self):
        signed = "Dear Team,\n\nHello.\n\nKind regards,\nSam Taylor\n+44 7700 900123 | linkedin.com/in/samtaylor"
        ready = self.app(status=Status.READY, body=signed)
        sent = self.app(status=Status.SENT, body=signed)
        sending = self.app(status=Status.SENDING, body=signed)
        edited = self.app(status=Status.READY, body=f"{signed}\nP.S. Call me any time.")
        other = self.app(status=Status.READY, body=signed, resume=Resume.objects.create(name="b.pdf", data=b"%PDF", text="x"))
        response = self.save(self.cv, phone="+92 333 0000000", linkedin="", facts="x")
        self.assertContains(response, "1 unsent email now signed with them too.")
        expected = {
            ready: "Kind regards,\nSam Taylor\n+92 333 0000000",
            sent: signed,
            sending: signed,
            edited: "P.S. Call me any time.",
            other: signed,
        }
        for app, ending in expected.items():
            with self.subTest(app.status):
                app.refresh_from_db()
                self.assertTrue(app.body.endswith(ending), app.body[-60:])

    def test_drafts_signed_under_an_earlier_name_are_re_signed_too(self):
        draft = self.app(status=Status.READY, body="Hello\n\nKind regards,\nSam T\n+44 7700 900123 | linkedin.com/in/samtaylor")
        self.save(self.cv, phone="+92 333 0000000", linkedin="", facts="x")
        draft.refresh_from_db()
        self.assertEqual(draft.body, "Hello\n\nKind regards,\nSam T\n+92 333 0000000")

    def test_a_draft_without_a_contact_line_gets_one(self):
        Resume.objects.filter(pk=self.cv.pk).update(phone="", linkedin="")
        draft = self.app(status=Status.MANUAL, body="Hello\n\nKind regards,\nSam Taylor")
        self.save(Resume.objects.get(pk=self.cv.pk), phone="+92 333 0000000", linkedin="", facts="x")
        draft.refresh_from_db()
        self.assertEqual(draft.body, "Hello\n\nKind regards,\nSam Taylor\n+92 333 0000000")

    def test_emails_that_may_have_gone_out_are_left_alone(self):
        signed = "Hello\n\nKind regards,\nSam Taylor\n+44 7700 900123 | linkedin.com/in/samtaylor"
        maybe = [
            self.app(status=Status.FAILED, body=signed, note=pipeline.INTERRUPTED_SEND),
            self.app(status=Status.NEEDS_YOU, body=signed, note=pipeline.INTERRUPTED_FORM),
            *[self.app(status=Status.NEEDS_YOU, body=signed, note=note) for note in pipeline.formfill.MAYBE_SUBMITTED],
        ]
        response = self.save(self.cv, phone="+92 333 0000000", linkedin="", facts="x")
        self.assertNotContains(response, "unsent email")
        for app in maybe:
            app.refresh_from_db()
            self.assertEqual(app.body, signed)

    def test_unchanged_details_leave_emails_alone(self):
        self.app(status=Status.READY, body="Hello\n\nKind regards,\nSam Taylor\n+44 7700 900123 | linkedin.com/in/samtaylor")
        response = self.save(self.cv, phone="+44 7700 900123", linkedin="linkedin.com/in/samtaylor", facts="x")
        self.assertNotContains(response, "unsent email")

    def test_details_can_be_cleared(self):
        self.save(self.cv, phone="", linkedin="", facts="")
        self.cv.refresh_from_db()
        self.assertEqual((self.cv.phone, self.cv.linkedin, self.cv.facts), ("", "", ""))

    def test_long_values_are_cut(self):
        self.save(self.cv, full_name="n" * 999, phone="1" * 99, linkedin="l" * 999, country="c" * 99, facts="f" * 9999)
        self.cv.refresh_from_db()
        self.assertEqual(
            (len(self.cv.full_name), len(self.cv.phone), len(self.cv.linkedin), len(self.cv.country), len(self.cv.facts)),
            (120, 40, 200, 60, 5000),
        )

    def test_saving_leaves_the_file_alone(self):
        self.save(self.cv, phone="1", facts="x")
        self.assertEqual(bytes(Resume.objects.get(pk=self.cv.pk).data), b"%PDF-1.4 fake cv bytes")

    def test_unknown_cv(self):
        self.assertEqual(self.client.post(reverse("save_cv", args=[9999]), {"phone": "1"}).status_code, 404)

    def test_each_cv_shows_its_own_details(self):
        Resume.objects.create(name="pk.pdf", data=b"%PDF", text="x", phone="+92 300 1234567", facts="Where I live now: Lahore")
        response = self.client.get(reverse("home"))
        for words in ("+44 7700 900123", "linkedin.com/in/samtaylor", "+92 300 1234567", "Where I live now: Lahore", "No LinkedIn"):
            with self.subTest(words):
                self.assertContains(response, words)
        self.assertContains(response, "Where I live now:\nRight to work")  # a CV without facts starts from the template
        self.assertContains(response, 'id="facts-%d"' % self.cv.pk)

    def test_details_are_escaped(self):
        Resume.objects.filter(pk=self.cv.pk).update(phone="<b>1</b>", linkedin='"><script>', facts="</textarea><script>")
        response = self.client.get(reverse("home"))
        self.assertNotContains(response, "<script>")
        self.assertNotContains(response, "<b>1</b>")


class ConnectGmailTests(ViewTestCase):
    def post(self, **data):
        return self.client.post(reverse("connect_gmail"), data, follow=True)

    def test_good_login_saved_to_env_file_without_spaces(self):
        with mock.patch("core.mailer.verify") as verify:
            response = self.post(address="New@Gmail.com ", password="wxyz wxyz wxyz wxyz")
        verify.assert_called_once_with("new@gmail.com", "wxyzwxyzwxyzwxyz")
        self.assertContains(response, "Gmail connected as new@gmail.com")
        saved = self.env_file.read_text()  # the first account stays where it was, and this one goes next to it
        self.assertIn("GMAIL_ADDRESS_2='new@gmail.com'", saved)
        self.assertIn("GMAIL_APP_PASSWORD_2='wxyzwxyzwxyzwxyz'", saved)
        self.assertEqual(mailer.accounts(), ["sam@gmail.com", "new@gmail.com"])
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)

    def test_the_first_account_goes_where_it_always_did(self):
        with mock.patch.dict(os.environ, {"GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": ""}), mock.patch("core.mailer.verify"):
            self.post(address="new@gmail.com", password="wxyzwxyzwxyzwxyz")
            self.assertIn("GMAIL_ADDRESS='new@gmail.com'", self.env_file.read_text())
            self.assertEqual(mailer.accounts(), ["new@gmail.com"])

    def test_bad_login_not_saved(self):
        with mock.patch("core.mailer.verify", side_effect=mailer.MailError("Gmail rejected the login")):
            response = self.post(address="new@gmail.com", password="wrong")
        self.assertContains(response, "Gmail rejected the login")
        self.assertFalse(self.env_file.exists())
        self.assertEqual(os.environ["GMAIL_ADDRESS"], "sam@gmail.com")

    def test_invalid_input(self):
        self.assertContains(self.post(address="nope", password="x"), "valid Gmail")
        self.assertContains(self.post(address="a@gmail.com", password="  "), "App Password")


class SaveGeminiTests(ViewTestCase):
    def post(self, key):
        return self.client.post(reverse("save_gemini"), {"key": key}, follow=True)

    def test_good_key_saved(self):
        with mock.patch("core.llm.verify_key") as verify:
            response = self.post(" AIza-new ")
        verify.assert_called_once_with("AIza-new")
        self.assertContains(response, "Gemini connected")
        self.assertIn("GEMINI_API_KEY='AIza-new'", self.env_file.read_text())
        self.assertNotContains(response, "AIza-new")

    def test_bad_key_not_saved(self):
        with mock.patch("core.llm.verify_key", side_effect=llm.LLMError("rejected the API key")):
            response = self.post("bad")
        self.assertContains(response, "rejected the API key")
        self.assertFalse(self.env_file.exists())

    def test_malformed_keys_refused_before_any_call(self):
        for key in ("", "   ", "has space", "ключ"):
            with self.subTest(key), mock.patch("core.llm.verify_key") as verify:
                self.assertContains(self.post(key), "valid Gemini")
                verify.assert_not_called()


class ApplyTests(ViewTestCase):
    def post(self, jd=JD_TEXT, action="send"):
        return self.client.post(reverse("apply_jd"), {"jd": jd, "action": action}, follow=True)

    def test_sends_straight_away(self):
        with mock.patch("core.llm.generate_json", return_value=gemini_job()):
            response = self.post()
        self.assertContains(response, "On it")
        self.assertEqual(Application.objects.get().status, Status.SENT)
        self.assertEqual(len(mail.outbox), 1)

    def test_draft_button_only_saves(self):
        with mock.patch("core.llm.generate_json", return_value=gemini_job()):
            self.post(action="draft")
        self.assertEqual(Application.objects.get().status, Status.READY)
        self.assertEqual(mail.outbox, [])

    def test_no_email_keeps_the_pasted_text(self):
        with mock.patch("core.llm.generate_json") as generate:
            response = self.post(LONG_JD_NO_EMAIL)
        self.assertContains(response, "No email address")
        self.assertContains(response, "Senior Python developer needed")
        generate.assert_not_called()
        self.assertEqual(Application.objects.count(), 0)

    def test_draft_allowed_without_email(self):
        with mock.patch("core.llm.generate_json", return_value=gemini_job(application_email="")):
            self.post(LONG_JD_NO_EMAIL, action="draft")
        app = Application.objects.get()
        self.assertEqual(app.status, Status.READY)
        self.assertEqual(app.recipient, "")
        self.assertIn("Add one below", app.note)

    def test_only_unusable_addresses_named_back(self):
        response = self.post(LONG_JD_NO_EMAIL + "Privacy: privacy@acme.com")
        self.assertContains(response, "privacy@acme.com")
        self.assertContains(response, "won&#x27;t take applications")

    def test_own_address_is_not_enough(self):
        response = self.post(LONG_JD_NO_EMAIL + "Contact sam@gmail.com")
        self.assertContains(response, "take applications")

    def test_too_short(self):
        response = self.post("Dev job a@b.com")
        self.assertContains(response, "whole job description")
        self.assertContains(response, "Dev job a@b.com")

    def test_empty(self):
        self.assertContains(self.post(""), "whole job description")

    def test_setup_incomplete(self):
        Resume.objects.all().delete()
        response = self.post()
        self.assertContains(response, "upload your CV")
        self.assertContains(response, "To apply, send your CV")
        self.assertEqual(Application.objects.count(), 0)

    def test_draft_doesnt_need_gmail(self):
        with mock.patch.dict(os.environ, {"GMAIL_APP_PASSWORD": ""}):
            with mock.patch("core.llm.generate_json", return_value=gemini_job()):
                self.assertNotContains(self.post(action="draft"), "connect Gmail first")
                self.assertEqual(Application.objects.get().status, Status.READY)

    def test_sending_needs_gmail(self):
        with mock.patch.dict(os.environ, {"GMAIL_APP_PASSWORD": ""}):
            response = self.post()
        self.assertContains(response, "First, connect Gmail")

    def test_duplicate_goes_to_the_existing_application(self):
        with mock.patch("core.llm.generate_json", return_value=gemini_job()) as generate:
            self.post()
            response = self.post()
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(response.redirect_chain[-1][0], reverse("detail", args=[Application.objects.get().pk]))
        self.assertContains(response, "already added")

    def test_very_long_jd_is_handled(self):
        with mock.patch("core.llm.generate_json", return_value=gemini_job()) as generate:
            self.post(JD_TEXT + " filler" * 40000)
        self.assertLess(len(generate.call_args.args[0]), 60_000)
        self.assertEqual(len(Application.objects.get().jd_text), 100_000)
        self.assertEqual(len(mail.outbox), 1)


class CrawlViewTests(ViewTestCase):
    def post(self, url, auto_send="on"):
        data = {"url": url}
        if auto_send:
            data["auto_send"] = auto_send
        return self.client.post(reverse("start_crawl"), data, follow=True)

    def test_starts_background_crawl(self):
        with mock.patch("core.pipeline.spawn") as spawn:
            response = self.post("https://board.example/jobs")
        run = CrawlRun.objects.get()
        spawn.assert_called_once()
        self.assertEqual(spawn.call_args.args[1], run.pk)
        self.assertTrue(run.auto_send)
        self.assertContains(response, "Crawl started")

    def test_adds_missing_scheme(self):
        with mock.patch("core.pipeline.spawn"):
            self.post("board.example/jobs")
        self.assertEqual(CrawlRun.objects.get().url, "https://board.example/jobs")

    def test_unticked_box_means_no_auto_send(self):
        with mock.patch("core.pipeline.spawn"):
            self.post("https://board.example/jobs", auto_send=None)
        self.assertFalse(CrawlRun.objects.get().auto_send)

    def test_rejects_invalid_links_and_keeps_them(self):
        for url in ("", "not a link", "javascript:alert(1)", "ftp://x.example/a", "https://"):
            with self.subTest(url), mock.patch("core.pipeline.spawn") as spawn:
                self.assertContains(self.post(url), "valid web link")
                spawn.assert_not_called()
        self.assertEqual(CrawlRun.objects.count(), 0)
        self.assertContains(self.post("not a link"), "not a link")

    def test_setup_incomplete(self):
        Resume.objects.all().delete()
        self.assertContains(self.post("https://board.example/jobs"), "upload your CV")
        self.assertEqual(CrawlRun.objects.count(), 0)

    def test_too_long(self):
        self.assertContains(self.post("https://board.example/" + "a" * 2000), "too long")
        self.assertEqual(CrawlRun.objects.count(), 0)


class DetailTests(ViewTestCase):
    def post_send(self, app, **overrides):
        data = {"recipient": "new@acme.co.uk", "subject": "New subject", "body": "New body"}
        data.update(overrides)
        return self.client.post(reverse("send", args=[app.pk]), data, follow=True)

    def test_not_found(self):
        self.assertEqual(self.client.get(reverse("detail", args=[999])).status_code, 404)
        self.assertEqual(self.client.post(reverse("retry", args=[999])).status_code, 404)
        self.assertEqual(self.client.post(reverse("send", args=[999])).status_code, 404)

    def test_ready_shows_editable_form(self):
        response = self.client.get(reverse("detail", args=[self.app().pk]))
        self.assertContains(response, 'name="recipient"')
        self.assertContains(response, "Sam_Taylor_CV.pdf")

    def test_sent_is_read_only(self):
        response = self.client.get(reverse("detail", args=[self.app(status=Status.SENT).pk]))
        self.assertNotContains(response, 'name="recipient"')
        self.assertNotContains(response, "Send now")
        self.assertNotContains(response, "Rewrite")

    def test_in_flight_refreshes_and_hides_forms(self):
        response = self.client.get(reverse("detail", args=[self.app(status=Status.PROCESSING).pk]))
        self.assertContains(response, 'http-equiv="refresh"')
        self.assertNotContains(response, "<form")

    def test_manual_job_shows_link_and_message(self):
        app = self.app(status=Status.MANUAL, recipient="", job_url="https://board.example/jobs/1")
        response = self.client.get(reverse("detail", args=[app.pk]))
        self.assertContains(response, "https://board.example/jobs/1")
        self.assertContains(response, "Hello")

    def test_edits_are_what_gets_sent(self):
        app = self.app()
        self.post_send(app)
        message = mail.outbox[0]
        self.assertEqual((message.to, message.subject, message.body), (["new@acme.co.uk"], "New subject", "New body"))
        app.refresh_from_db()
        self.assertEqual(app.status, Status.SENT)

    def test_manual_job_can_be_sent_once_an_address_is_typed(self):
        self.post_send(self.app(status=Status.MANUAL, recipient=""))
        self.assertEqual(mail.outbox[0].to, ["new@acme.co.uk"])

    def test_skipped_job_can_be_overridden(self):
        self.post_send(self.app(status=Status.SKIPPED))
        self.assertEqual(len(mail.outbox), 1)

    def test_sent_cant_be_resent(self):
        app = self.app(status=Status.SENT)
        response = self.post_send(app)
        self.assertContains(response, "can&#x27;t be sent")
        self.assertEqual(mail.outbox, [])
        app.refresh_from_db()
        self.assertEqual(app.subject, "Hi")

    def test_invalid_edits_refused(self):
        app = self.app()
        self.assertContains(self.post_send(app, recipient="nope"), "valid recipient")
        self.assertContains(self.post_send(app, subject="  "), "can&#x27;t be empty")
        self.assertContains(self.post_send(app, body="  "), "can&#x27;t be empty")
        self.assertEqual(mail.outbox, [])

    def test_retry_runs_tailoring_again(self):
        app = self.app(status=Status.FAILED, body="", recipient="", jd_text=JD_TEXT, candidates=["careers@acme.co.uk"])
        with mock.patch("core.llm.generate_json", return_value=gemini_job()):
            self.client.post(reverse("retry", args=[app.pk]))
        app.refresh_from_db()
        self.assertEqual(app.status, Status.SENT)

    def test_rewriting_a_draft_never_sends_it(self):
        app = self.app(jd_text=JD_TEXT, candidates=["careers@acme.co.uk"], auto_send=True)
        with mock.patch("core.llm.generate_json", return_value=gemini_job()):
            self.client.post(reverse("retry", args=[app.pk]))
        app.refresh_from_db()
        self.assertEqual(app.status, Status.READY)
        self.assertEqual(mail.outbox, [])

    def test_retry_on_old_row_isnt_mistaken_for_interrupted_work(self):
        from datetime import timedelta
        from core import pipeline
        app = self.app(status=Status.FAILED, body="", jd_text=JD_TEXT)
        Application.objects.filter(pk=app.pk).update(updated_at=pipeline.BOOT_TIME - timedelta(hours=1))
        with mock.patch("core.pipeline.spawn"):
            self.client.post(reverse("retry", args=[app.pk]))
            self.client.get(reverse("activity"))
        app.refresh_from_db()
        self.assertEqual(app.status, Status.QUEUED)

    def test_cant_retry_sent_or_running(self):
        for status in (Status.SENT, Status.SENDING, Status.PROCESSING, Status.QUEUED):
            with self.subTest(status), mock.patch("core.pipeline.spawn") as spawn:
                app = self.app(status=status)
                self.client.post(reverse("retry", args=[app.pk]))
                spawn.assert_not_called()
