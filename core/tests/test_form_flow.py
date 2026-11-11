"""The dashboard and pipeline around the form filler: who gets queued, what status results, what's stopped."""
import os
from datetime import timedelta
from unittest import SkipTest, mock

from django.core import mail
from django.urls import reverse

from core import formfill, llm, pipeline, prompts
from core.crawler import Page
from core.models import Application, CrawlRun, Profile, Resume
from core.tests.base import CV_TEXT, AppTestCase, RealBrowserTestCase, gemini_job
from core.tests.fake_site import FakeSite
from core.tests.test_formfill import chrome_available

Status = Application.Status
Source = Application.Source
Channel = Application.Channel
REAL_ENSURE_WORKER = pipeline.ensure_form_worker  # the test base replaces it, so no test opens Chrome by accident


class FlowMixin:
    def setUp(self):
        super().setUp()
        self.make_profile()

    def app(self, **fields):
        values = {
            "source_hash": os.urandom(16).hex(),
            "source": Source.CRAWL,
            "status": Status.MANUAL,
            "title": "Dev",
            "subject": "Hi",
            "body": "Dear Team, please see my CV.",
            "job_url": "https://board.example/jobs/1",
            "jd_text": "A remote developer job.",
            "auto_send": True,
            "resume": Resume.objects.first(),
        }
        values.update(fields)
        return Application.objects.create(**values)


class FlowTestCase(FlowMixin, AppTestCase):
    pass


class RoutingTests(FlowTestCase):
    JOB = "https://board.example/jobs/1"

    def setUp(self):
        super().setUp()
        self.page = Page(url=self.JOB, text="Dev job. Apply on our website.")
        fetcher = mock.Mock()
        fetcher.fetch.return_value = self.page
        patcher = mock.patch("core.pipeline.crawler.Crawler", return_value=fetcher)
        patcher.start()
        self.addCleanup(patcher.stop)

    def crawl(self, *answers, auto_send=True):
        run = CrawlRun.objects.create(url=self.JOB, auto_send=auto_send)
        with mock.patch("core.llm.generate_json", side_effect=[{"page_type": "single_job", "job_links": []}, *answers]):
            pipeline.run_crawl(run.pk)
        return Application.objects.get()

    def test_no_email_goes_to_the_form_queue(self):
        app = self.crawl(gemini_job(application_email=""))
        self.assertEqual(app.status, Status.FORM_QUEUE)
        pipeline.ensure_form_worker.assert_called()
        self.assertEqual(mail.outbox, [])

    def test_email_still_preferred_over_a_form(self):
        self.page.emails = ["jobs@acme.co.uk"]
        app = self.crawl(gemini_job(application_email="jobs@acme.co.uk"))
        self.assertEqual(app.status, Status.SENT)
        self.assertEqual(app.channel, Channel.EMAIL)

    def test_auto_off_waits_for_the_user(self):
        app = self.crawl(gemini_job(application_email=""), auto_send=False)
        self.assertEqual(app.status, Status.MANUAL)
        pipeline.ensure_form_worker.assert_not_called()

    def test_low_match_never_reaches_a_form(self):
        self.assertEqual(self.crawl(gemini_job(application_email="", match_score=10)).status, Status.SKIPPED)

    def test_not_remote_never_reaches_a_form(self):
        self.assertEqual(self.crawl(gemini_job(application_email="", rules_met="no")).status, Status.SKIPPED)

    def test_flagged_draft_never_reaches_a_form(self):
        bad = gemini_job(application_email="", body="Dear [Hiring Manager], " + "word " * 60)
        app = self.crawl(bad, bad)
        self.assertEqual(app.status, Status.MANUAL)
        self.assertFalse(app.auto_send)

    def test_crawl_summary_counts_forms(self):
        self.crawl(gemini_job(application_email=""))
        self.assertIn("1 going through website forms", CrawlRun.objects.get().note)


class FillFormResultTests(FlowTestCase):
    """_fill_form with the browser part replaced, checking what each result does to the application."""

    def run_with(self, outcome, app=None, allowed=True):
        app = app or self.app(status=Status.FORM_QUEUE)
        crawler = mock.Mock()
        crawler.allowed.return_value = allowed
        with mock.patch("core.pipeline.crawler.Crawler", return_value=crawler):
            with mock.patch("core.pipeline.formfill.apply", return_value=outcome) as apply:
                pipeline._fill_form(app, mock.Mock())
        app.refresh_from_db()
        self.apply_call = apply
        return app

    def test_submitted(self):
        answers = [{"label": "Email", "answer": "sam@gmail.com", "source": "contact"}]
        app = self.run_with(formfill.Outcome(formfill.SUBMITTED, "The site confirmed it", answers))
        self.assertEqual((app.status, app.channel), (Status.SENT, Channel.FORM))
        self.assertIsNotNone(app.sent_at)
        self.assertEqual(app.form_answers, answers)
        self.assertIn("confirmed", app.note)

    def test_needs_you(self):
        app = self.run_with(formfill.Outcome(formfill.NEEDS_YOU, "Needs you: no answer"))
        self.assertEqual((app.status, app.note), (Status.NEEDS_YOU, "Needs you: no answer"))
        self.assertIsNone(app.sent_at)

    def test_no_form(self):
        app = self.run_with(formfill.Outcome(formfill.NO_FORM, "Couldn't find an application form."))
        self.assertEqual(app.status, Status.MANUAL)
        self.assertIn("Open the link", app.note)

    def test_robots_refusal_never_opens_the_browser(self):
        app = self.run_with(None, allowed=False)
        self.assertEqual(app.status, Status.NEEDS_YOU)
        self.assertIn("robots.txt", app.note)
        self.apply_call.assert_not_called()

    def test_crash_never_leaves_it_stuck(self):
        app = self.app(status=Status.FORM_QUEUE)
        crawler = mock.Mock()
        crawler.allowed.side_effect = RuntimeError("surprise")
        with mock.patch("core.pipeline.crawler.Crawler", return_value=crawler):
            pipeline._fill_form(app, mock.Mock())
        app.refresh_from_db()
        self.assertEqual(app.status, Status.NEEDS_YOU)
        self.assertIn("surprise", app.note)

    def test_automatic_run_may_submit_and_closes_its_window(self):
        self.run_with(formfill.Outcome(formfill.NEEDS_YOU))
        job = self.apply_call.call_args.args[1]
        self.assertTrue(job.submit)
        self.assertFalse(job.stay_open)

    def test_button_press_never_submits_and_leaves_the_window(self):
        self.run_with(formfill.Outcome(formfill.NEEDS_YOU), app=self.app(status=Status.FORM_QUEUE, auto_send=False))
        job = self.apply_call.call_args.args[1]
        self.assertFalse(job.submit)
        self.assertTrue(job.stay_open)

    def test_daily_limit_stops_the_submit(self):
        with mock.patch.dict(os.environ, {"DAILY_SEND_LIMIT": "0"}):
            app = self.run_with(formfill.Outcome(formfill.NEEDS_YOU, "The form is filled in."))
        self.assertFalse(self.apply_call.call_args.args[1].submit)
        self.assertIn("Daily limit", app.note)

    def test_form_submissions_count_towards_the_daily_limit(self):
        self.run_with(formfill.Outcome(formfill.SUBMITTED))
        self.assertEqual(pipeline.sent_today(), 1)

    def test_job_carries_the_right_details(self):
        Profile.objects.filter(pk=1).update(allow_consent=True)
        self.run_with(formfill.Outcome(formfill.NEEDS_YOU))
        job = self.apply_call.call_args.args[1]
        self.assertEqual(job.full_name, "Sam Taylor")
        self.assertEqual(job.email, "sam@gmail.com")
        self.assertEqual(job.resume_name, "Sam_Taylor_CV.pdf")
        self.assertEqual(job.resume_bytes, b"%PDF-1.4 fake cv bytes")
        self.assertEqual(job.cover_name, "Sam_Taylor_Cover_Letter.docx")
        self.assertTrue(job.cover_bytes.startswith(b"PK"))
        self.assertTrue(job.allow_consent)
        self.assertEqual((job.phone, job.linkedin), ("+44 7700 900123", "linkedin.com/in/samtaylor"))

    def test_job_carries_the_chosen_cvs_details_and_facts(self):
        pk = Resume.objects.create(
            name="pk.pdf", data=b"%PDF pk", text="AI engineer. " * 30, phone="+92 333 0000000",
            facts="Where I live now: Lahore, Pakistan", full_name="Muhammad Sam", email="sam.pk@gmail.com",
            links="https://github.com/sampk\nhttps://sampk.dev",
        )
        Resume.objects.filter(pk=self.cv.pk).update(facts="Where I live now: Leeds, United Kingdom")
        self.run_with(formfill.Outcome(formfill.NEEDS_YOU), app=self.app(status=Status.FORM_QUEUE, resume=pk))
        job = self.apply_call.call_args.args[1]
        self.assertEqual((job.phone, job.linkedin, job.resume_bytes), ("+92 333 0000000", "", b"%PDF pk"))
        self.assertEqual((job.full_name, job.email, job.github), ("Muhammad Sam", "sam.pk@gmail.com", "https://github.com/sampk"))
        self.assertEqual((job.resume_name, job.cover_name), ("Muhammad_Sam_CV.pdf", "Muhammad_Sam_Cover_Letter.docx"))
        with mock.patch("core.llm.generate_json", return_value={"answers": []}) as generate:
            job.ask([])
        prompt = generate.call_args.args[0]
        self.assertIn("Lahore, Pakistan", prompt)
        self.assertIn("<links>\nhttps://github.com/sampk\nhttps://sampk.dev\n</links>", prompt)
        self.assertIn("Candidate name: Muhammad Sam", prompt)
        self.assertNotIn("Leeds", prompt)

    def test_row_that_is_no_longer_queued_is_left_alone(self):
        app = self.app(status=Status.SENT)
        with mock.patch("core.pipeline.formfill.apply") as apply:
            pipeline._fill_form(app, mock.Mock())
        apply.assert_not_called()


class AskGeminiTests(FlowTestCase):
    FIELD = formfill.Field("f1", "text", "text", "Current company", True)

    def test_prompt_contains_the_facts_and_question(self):
        self.cv.facts = "I live in Karachi."
        self.cv.save()
        answer = {"answers": [{"id": "f1", "values": ["Northwind"], "source": "CV"}]}
        with mock.patch("core.llm.generate_json", return_value=answer) as generate:
            replies = pipeline._ask_gemini(self.app(), self.cv, [self.FIELD])
        prompt = generate.call_args.args[0]
        self.assertIn("I live in Karachi.", prompt)
        self.assertIn(CV_TEXT.strip()[:50], prompt)
        self.assertIn("Current company", prompt)
        self.assertEqual(replies["f1"].values, ["Northwind"])
        self.assertEqual(replies["f1"].source, "cv")

    def test_failure_becomes_a_form_error(self):
        with mock.patch("core.llm.generate_json", side_effect=llm.LLMError("quota")):
            with self.assertRaisesMessage(formfill.FormError, "quota"):
                pipeline._ask_gemini(self.app(), self.cv, [self.FIELD])

    def test_odd_answers_cant_crash_it(self):
        for raw in ({}, {"answers": None}, {"answers": ["x"]}, {"answers": [{"id": "f1", "values": "text"}]}):
            with self.subTest(raw), mock.patch("core.llm.generate_json", return_value=raw):
                self.assertEqual(pipeline._ask_gemini(self.app(), self.cv, [self.FIELD]), {})


class WorkerTests(FlowTestCase):
    def setUp(self):
        super().setUp()
        self.chrome = mock.MagicMock()
        self.chrome.__enter__.return_value = self.chrome
        self.chrome.windows_open.return_value = False
        patcher = mock.patch("core.pipeline.formfill.Chrome", return_value=self.chrome)
        patcher.start()
        self.addCleanup(patcher.stop)
        crawler = mock.patch("core.pipeline.crawler.Crawler")
        crawler.start()
        self.addCleanup(crawler.stop)

    def test_works_through_the_queue_oldest_first(self):
        first, second = self.app(status=Status.FORM_QUEUE), self.app(status=Status.FORM_QUEUE)
        order = []

        def apply(chrome, job):
            order.append(job.url)
            return formfill.Outcome(formfill.SUBMITTED)

        Application.objects.filter(pk=first.pk).update(job_url="https://a.example/1", updated_at=pipeline.BOOT_TIME)
        with mock.patch("core.pipeline.formfill.apply", side_effect=apply):
            pipeline._form_worker()
        self.assertEqual(order, ["https://a.example/1", second.job_url])
        self.assertEqual(Application.objects.filter(status=Status.SENT).count(), 2)

    def test_waits_for_windows_left_open(self):
        self.chrome.windows_open.side_effect = [True, True, False]
        pipeline._form_worker()
        self.assertEqual(self.chrome.wait.call_count, 2)

    def test_chrome_missing_puts_the_queue_back_with_a_reason(self):
        app = self.app(status=Status.FORM_QUEUE)
        with mock.patch("core.pipeline.formfill.Chrome", side_effect=formfill.FormError("Install Chrome")):
            pipeline._form_worker()
        app.refresh_from_db()
        self.assertEqual((app.status, app.note), (Status.MANUAL, "Install Chrome"))
        self.assertFalse(pipeline._worker_running)

    def test_any_worker_crash_cant_leave_the_queue_spinning(self):
        app = self.app(status=Status.FORM_QUEUE)
        with mock.patch("core.pipeline.formfill.Chrome", side_effect=RuntimeError("no driver")):
            pipeline._form_worker()
        app.refresh_from_db()
        self.assertEqual(app.status, Status.MANUAL)


class EnsureWorkerTests(FlowTestCase):
    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, pipeline, "_worker_running", False)
        patcher = mock.patch("core.pipeline._form_worker")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_does_nothing_without_a_queue(self):
        REAL_ENSURE_WORKER()
        pipeline._form_worker.assert_not_called()

    def test_starts_for_a_queue(self):
        self.app(status=Status.FORM_QUEUE)
        REAL_ENSURE_WORKER()
        pipeline._form_worker.assert_called_once()

    def test_only_one_worker_at_a_time(self):
        self.app(status=Status.FORM_QUEUE)
        REAL_ENSURE_WORKER()
        REAL_ENSURE_WORKER()
        pipeline._form_worker.assert_called_once()


class RecoveryTests(FlowTestCase):
    def test_interrupted_form_goes_to_the_user_not_back_in_the_queue(self):
        app = self.app(status=Status.FILLING)
        Application.objects.filter(pk=app.pk).update(updated_at=pipeline.BOOT_TIME - timedelta(seconds=1))
        pipeline.recover_interrupted()
        app.refresh_from_db()
        self.assertEqual(app.status, Status.NEEDS_YOU)
        self.assertIn("may have been submitted", app.note)

    def test_recrawling_never_requeues_a_form_that_needs_you(self):
        app = self.app(status=Status.NEEDS_YOU, source_hash=pipeline.source_hash("https://board.example/jobs/1"))
        again, is_new = pipeline._claim(app.source_hash, source=Source.CRAWL, crawl=None, job_url=app.job_url, auto_send=True, jd_text="", candidates=[])
        self.assertFalse(is_new)
        self.assertEqual(again.status, Status.NEEDS_YOU)


class ViewTests(FlowTestCase):
    def test_fill_button_queues_it_for_the_user_to_submit(self):
        app = self.app(status=Status.MANUAL, auto_send=True)
        response = self.client.post(reverse("fill_form", args=[app.pk]), follow=True)
        app.refresh_from_db()
        self.assertEqual((app.status, app.auto_send), (Status.FORM_QUEUE, False))
        pipeline.ensure_form_worker.assert_called()
        self.assertContains(response, 'http-equiv="refresh"')

    def test_fill_button_refused_when_it_cant_run(self):
        for fields in ({"status": Status.SENT}, {"status": Status.FILLING}, {"job_url": ""}, {"body": ""}):
            with self.subTest(fields):
                app = self.app(**fields)
                before = app.status
                response = self.client.post(reverse("fill_form", args=[app.pk]), follow=True)
                app.refresh_from_db()
                self.assertEqual(app.status, before)
                self.assertContains(response, "can&#x27;t be filled")

    def test_mark_applied(self):
        for status in (Status.NEEDS_YOU, Status.MANUAL):
            with self.subTest(status):
                app = self.app(status=status)
                self.client.post(reverse("mark_applied", args=[app.pk]))
                app.refresh_from_db()
                self.assertEqual((app.status, app.channel), (Status.SENT, Channel.FORM))
                self.assertIsNotNone(app.sent_at)

    def test_mark_applied_refused_from_other_states(self):
        for status in (Status.SENT, Status.QUEUED, Status.FAILED, Status.READY):
            with self.subTest(status):
                app = self.app(status=status)
                self.client.post(reverse("mark_applied", args=[app.pk]))
                app.refresh_from_db()
                self.assertEqual(app.status, status)
                self.assertIsNone(app.sent_at)

    def test_new_routes_refuse_get_and_unknown_rows(self):
        app = self.app()
        for name in ("fill_form", "mark_applied"):
            with self.subTest(name):
                self.assertEqual(self.client.get(reverse(name, args=[app.pk])).status_code, 405)
                self.assertEqual(self.client.post(reverse(name, args=[9999])).status_code, 404)
        self.assertEqual(self.client.get(reverse("save_setup")).status_code, 405)

    def test_detail_offers_the_right_buttons(self):
        waiting = self.client.get(reverse("detail", args=[self.app(status=Status.NEEDS_YOU).pk]))
        self.assertContains(waiting, "Fill the form")
        self.assertContains(waiting, "I submitted it")
        done = self.client.get(reverse("detail", args=[self.app(status=Status.SENT, channel=Channel.FORM).pk]))
        self.assertNotContains(done, "Fill the form")
        self.assertNotContains(done, "I submitted it")
        pasted = self.client.get(reverse("detail", args=[self.app(status=Status.READY, source=Source.PASTE, job_url="").pk]))
        self.assertNotContains(pasted, "Fill the form")

    def test_form_answers_shown_and_escaped(self):
        answers = [{"label": "<script>alert(1)</script>", "answer": "<img src=x onerror=alert(1)>", "source": "cv"}]
        response = self.client.get(reverse("detail", args=[self.app(status=Status.SENT, channel=Channel.FORM, form_answers=answers).pk]))
        self.assertContains(response, "What went into the form")
        self.assertNotContains(response, "<script>alert")
        self.assertNotContains(response, "<img src=x")

    def test_activity_refreshes_while_forms_are_running(self):
        for status in (Status.FORM_QUEUE, Status.FILLING):
            with self.subTest(status):
                app = self.app(status=status)
                self.assertContains(self.client.get(reverse("activity")), 'http-equiv="refresh"')
                app.delete()
        self.app(status=Status.NEEDS_YOU)
        response = self.client.get(reverse("activity"))
        self.assertNotContains(response, 'http-equiv="refresh"')
        self.assertEqual(dict((key, count) for key, _, count in response.context["tabs"])["you"], 1)

    def test_activity_restarts_a_queue_left_after_a_restart(self):
        self.app(status=Status.FORM_QUEUE)
        self.client.get(reverse("activity"))
        pipeline.ensure_form_worker.assert_called()

    def test_facts_saved_and_shown(self):
        self.save_setup(facts="I live in Karachi.\r\nNo sponsorship needed.")
        self.cv.refresh_from_db()
        self.assertEqual(self.cv.facts, "I live in Karachi.\nNo sponsorship needed.")
        self.assertContains(self.client.get(reverse("home")), "I live in Karachi.")

    def test_consent_box_off_by_default_and_can_be_turned_off(self):
        self.assertFalse(Profile.load().allow_consent)
        self.save_setup(allow_consent=True)
        self.assertTrue(Profile.load().allow_consent)
        self.save_setup(allow_consent=False)
        self.assertFalse(Profile.load().allow_consent)

    def test_untouched_template_isnt_saved_as_facts(self):
        self.save_setup(facts=prompts.FACTS_TEMPLATE.replace("\n", "\r\n"))
        self.cv.refresh_from_db()
        self.assertEqual(self.cv.facts, "")
        self.assertContains(self.client.get(reverse("home")), "Where I live now:")

    def test_facts_are_sent_with_every_email(self):
        Resume.objects.filter(pk=self.cv.pk).update(facts="I live in Karachi.")
        app, _ = pipeline.submit_jd("A job. Apply to jobs@acme.co.uk. " * 5, False)
        with mock.patch("core.llm.generate_json", return_value=gemini_job(application_email="jobs@acme.co.uk")) as generate:
            pipeline.process(app.pk)
        self.assertIn("I live in Karachi.", generate.call_args.args[0])

    def test_email_rules_guard_against_implying_uk_residence(self):
        self.assertIn("Where the candidate lives comes only from <facts>", prompts.TAILOR_SYSTEM)
        self.assertIn("If the job is in or limited to a country where the CV shows work experience", prompts.TAILOR_SYSTEM)
        self.assertIn("never where the candidate lives or may work", prompts.TAILOR_SYSTEM)


class EndToEndTests(FlowMixin, RealBrowserTestCase):
    """Dashboard to employer, with the real worker and real Chrome. Only Gemini is faked."""

    def setUp(self):
        super().setUp()
        if not chrome_available():
            raise SkipTest("Google Chrome isn't installed")
        self.site = FakeSite()
        self.addCleanup(self.site.close)
        for patcher in (
            mock.patch("core.formfill.is_public", return_value=True),
            mock.patch("core.pipeline.HEADLESS", True),
            mock.patch("core.pipeline.crawler.Crawler"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_form_submitted_and_recorded(self):
        def fake(prompt, schema, system=""):
            key = next(line.split(" | ")[0] for line in prompt.splitlines() if "Why do you want" in line)
            return {"answers": [{"id": key, "values": ["Because of the Django work."], "source": "writing"}]}

        app = self.app(status=Status.FORM_QUEUE, job_url=self.site.url + "/plain")
        with mock.patch("core.llm.generate_json", side_effect=fake):
            pipeline._form_worker()
        app.refresh_from_db()
        self.assertEqual((app.status, app.channel), (Status.SENT, Channel.FORM), app.note)
        sent = self.site.submissions[0]
        self.assertEqual(sent["why"], "Because of the Django work.")
        self.assertEqual(sent["resume"], ("Sam_Taylor_CV.pdf", b"%PDF-1.4 fake cv bytes"))
        self.assertTrue(any(a["answer"] == "Because of the Django work." for a in app.form_answers))

    def test_blank_answers_stop_it_and_nothing_reaches_the_employer(self):
        app = self.app(status=Status.FORM_QUEUE, job_url=self.site.url + "/plain")
        with mock.patch("core.llm.generate_json", return_value={"answers": []}):
            pipeline._form_worker()
        app.refresh_from_db()
        self.assertEqual(app.status, Status.NEEDS_YOU)
        self.assertIn("Why do you want this job?", app.note)
        self.assertEqual(self.site.submissions, [])
        self.assertTrue(any(a["answer"] == "Sam" for a in app.form_answers))
