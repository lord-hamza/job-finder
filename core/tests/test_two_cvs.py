"""Two CVs: which one is used, which jobs each may apply to, and what goes out with it."""
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from core import formfill, pipeline, prompts
from core.crawler import Page
from core.models import Application, CrawlRun, Profile
from core.tests.base import CV_TEXT, GOOD_BODY, JD_TEXT, AppTestCase, gemini_job

Status = Application.Status
UK, PK = Profile.Market.UK, Profile.Market.PAKISTAN
PK_CV = "Sam Taylor\n+92 300 1234567\nAI Engineer building LLM and RAG systems in Python. " * 3
LAHORE_JOB = (
    "AI Engineer, Lahore. This is a hybrid role, with 3 days a week in the office. You will build LLM applications with Python. "
    "To apply, send your CV to careers@acme.pk."
)


class TwoCvTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.uk = self.make_profile()
        self.pk = Profile.load(PK)
        self.pk.full_name = "Sam Taylor"
        self.pk.phone = "+92 300 1234567"
        self.pk.linkedin = "linkedin.com/in/sam-pk"
        self.pk.resume_name = "ai engineer.pdf"
        self.pk.resume_bytes = b"%PDF pakistan cv"
        self.pk.resume_text = PK_CV
        self.pk.save()

    def gemini(self, *answers):
        patcher = mock.patch("core.llm.generate_json", side_effect=list(answers))
        self.addCleanup(patcher.stop)
        return patcher.start()

    def submit(self, text, markets, auto_send=True):
        app, _ = pipeline.submit_jd(text, auto_send, markets)
        pipeline.process(app.pk)
        app.refresh_from_db()
        return app


class ChoosingTests(TwoCvTestCase):
    def test_both_ticked_uses_the_cv_gemini_picks(self):
        generate = self.gemini(gemini_job(cv_id="pk", application_email="careers@acme.pk"))
        app = self.submit(LAHORE_JOB, [UK, PK])
        self.assertEqual(generate.call_count, 1, "one call, even with two CVs")
        self.assertEqual((app.profile, app.status), (self.pk, Status.SENT))
        message = mail.outbox[0]
        self.assertEqual(message.attachments[0][1], b"%PDF pakistan cv")
        self.assertIn("+92 300 1234567 | linkedin.com/in/sam-pk", message.body)
        self.assertNotIn("+44", message.body)

    def test_both_cvs_and_both_sets_of_rules_reach_gemini(self):
        generate = self.gemini(gemini_job(cv_id="uk"))
        self.submit(JD_TEXT, [UK, PK])
        prompt = generate.call_args.args[0]
        for words in (CV_TEXT.strip()[:50], "AI Engineer building LLM", 'cv_option id="uk"', 'cv_option id="pk"', "fully remote jobs open", "Lahore or Islamabad"):
            with self.subTest(words):
                self.assertIn(words, prompt)

    def test_each_cv_brings_its_own_facts(self):
        Profile.objects.filter(market="pk").update(facts="Where I live now: Lahore")
        generate = self.gemini(gemini_job(cv_id="uk"))
        self.submit(JD_TEXT, [UK, PK])
        prompt = generate.call_args.args[0]
        self.assertIn("Where I live now: United Kingdom", prompt)
        self.assertIn("Where I live now: Lahore", prompt)

    def test_only_the_ticked_cv_is_shown_and_used(self):
        for market, kept, gone in ((UK, "fully remote jobs open", "AI Engineer building LLM"), (PK, "AI Engineer building LLM", CV_TEXT.strip()[:50])):
            with self.subTest(market):
                Application.objects.all().delete()
                generate = self.gemini(gemini_job(cv_id="uk" if market == PK else "pk", application_email=""))
                app = self.submit(JD_TEXT + " AI Lahore", [market], auto_send=False)
                prompt = generate.call_args.args[0]
                self.assertIn(kept, prompt)
                self.assertNotIn(gone, prompt)
                self.assertEqual(app.profile.market, market, "a CV that wasn't ticked is never used")

    def test_cv_that_is_ruled_out_gets_the_other_cv_tried(self):
        generate = self.gemini(
            gemini_job(cv_id="pk", rules_met="no"),
            gemini_job(cv_id="uk", rules_met="yes", match_score=55),
        )
        app = self.submit(JD_TEXT, [UK, PK])
        self.assertEqual(generate.call_count, 2)
        self.assertEqual((app.profile, app.status), (self.uk, Status.SENT))
        second = generate.call_args_list[1].args[0]
        self.assertIn('cv_option id="uk"', second)
        self.assertNotIn('cv_option id="pk"', second)
        self.assertEqual(mail.outbox[0].attachments[0][1], b"%PDF-1.4 fake cv bytes")

    def test_posting_wording_also_gets_the_other_cv_tried(self):
        self.gemini(gemini_job(cv_id="uk", application_email="careers@acme.pk"), gemini_job(cv_id="pk", application_email="careers@acme.pk"))
        app = self.submit(LAHORE_JOB, [UK, PK])
        self.assertEqual((app.profile, app.status), (self.pk, Status.SENT))

    def test_both_ruled_out_stays_skipped_with_the_first_reason(self):
        generate = self.gemini(gemini_job(cv_id="pk", rules_met="no"), gemini_job(cv_id="uk", rules_met="no"))
        app = self.submit(JD_TEXT, [UK, PK])
        self.assertEqual(generate.call_count, 2)
        self.assertEqual((app.profile, app.status), (self.pk, Status.SKIPPED))
        self.assertIn("Lahore or Islamabad", app.note)
        self.assertEqual(mail.outbox, [])

    def test_gemini_failing_on_the_second_look_keeps_the_first_answer(self):
        from core import llm
        self.gemini(gemini_job(cv_id="pk", rules_met="no"), llm.LLMError("busy"))
        app = self.submit(JD_TEXT, [UK, PK])
        self.assertEqual((app.status, app.profile), (Status.SKIPPED, self.pk))

    def test_no_second_call_when_the_first_cv_is_fine_or_only_one_is_ticked(self):
        generate = self.gemini(gemini_job(cv_id="uk"))
        self.submit(JD_TEXT, [UK, PK])
        self.assertEqual(generate.call_count, 1)
        Application.objects.all().delete()
        generate = self.gemini(gemini_job(cv_id="uk", rules_met="no"))
        self.submit(JD_TEXT, [UK])
        self.assertEqual(generate.call_count, 1)

    def test_unknown_or_missing_choice_falls_back_to_the_first_ticked(self):
        for answer in ("mars", None):
            with self.subTest(answer):
                Application.objects.all().delete()
                self.gemini(gemini_job(cv_id=answer))
                self.assertEqual(self.submit(JD_TEXT, [UK, PK]).profile, self.uk)

    def test_a_ticked_cv_that_was_never_uploaded_is_left_out(self):
        Profile.objects.filter(market="pk").update(resume_bytes=b"", resume_text="")
        generate = self.gemini(gemini_job(cv_id="pk"))
        app = self.submit(JD_TEXT, [UK, PK])
        self.assertEqual(app.profile, self.uk)
        self.assertNotIn('cv_option id="pk"', generate.call_args.args[0])

    def test_older_jobs_with_no_choice_recorded_use_the_uk_cv(self):
        app = Application.objects.create(source_hash="a" * 64, source="paste", jd_text=JD_TEXT, candidates=["careers@acme.co.uk"], status=Status.QUEUED)
        self.gemini(gemini_job())
        pipeline.process(app.pk)
        app.refresh_from_db()
        self.assertEqual(app.profile, self.uk)


class PakistanRuleTests(TwoCvTestCase):
    def apply(self, text, **answer):
        values = {"cv_id": "pk", "rules_met": "yes", "application_email": "careers@acme.pk"}
        values.update(answer)
        self.gemini(gemini_job(**values))
        return self.submit(text, [PK])

    def test_hybrid_and_on_site_in_lahore_or_islamabad_are_fine(self):
        for text in (LAHORE_JOB, LAHORE_JOB.replace("Lahore", "Islamabad"), "On-site machine learning engineer in Islamabad. Apply to careers@acme.pk. " * 2):
            with self.subTest(text[:30]):
                Application.objects.all().delete()
                mail.outbox.clear()
                self.assertEqual(self.apply(text).status, Status.SENT)

    def test_remote_open_to_pakistan_is_fine(self):
        self.assertEqual(self.apply("Remote AI Engineer, anywhere in Pakistan. Apply to careers@acme.pk. " * 2).status, Status.SENT)

    def test_other_cities_are_refused(self):
        app = self.apply(LAHORE_JOB.replace("Lahore", "Karachi"), rules_met="no")
        self.assertEqual(app.status, Status.SKIPPED)
        self.assertIn("Lahore or Islamabad", app.note)
        self.assertEqual(mail.outbox, [])

    def test_posting_that_never_mentions_either_city_or_remote_is_refused_whatever_gemini_says(self):
        app = self.apply(LAHORE_JOB.replace("Lahore", "Karachi"))
        self.assertEqual(app.status, Status.SKIPPED)
        self.assertIn("doesn't mention Lahore, Islamabad or remote", app.note)
        self.assertEqual(mail.outbox, [])

    def test_job_that_never_mentions_ai_is_refused_whatever_gemini_says(self):
        app = self.apply("Accountant, Lahore. Prepare monthly accounts for our Lahore office. Send your CV to careers@acme.pk. " * 2)
        self.assertEqual(app.status, Status.SKIPPED)
        self.assertIn("AI or machine learning", app.note)
        self.assertEqual(mail.outbox, [])

    def test_words_that_only_contain_ai_or_ml_dont_count(self):
        self.assertEqual(pipeline._rule_problem(PK, "Email retail training in Lahore, html"), "The posting doesn't mention AI or machine learning work")

    def test_posting_that_doesnt_say_where_is_refused(self):
        app = self.apply(LAHORE_JOB, rules_met="unclear")
        self.assertEqual(app.status, Status.SKIPPED)
        self.assertIn("doesn't say where", app.note)

    def test_low_match_still_stops_a_crawl(self):
        run = CrawlRun.objects.create(url="https://acme.pk/jobs/1", markets=[PK])
        page = Page(url="https://acme.pk/jobs/1", text=LAHORE_JOB, emails=["careers@acme.pk"])
        fetcher = mock.Mock()
        fetcher.fetch.return_value = page
        self.gemini({"page_type": "single_job", "job_links": []}, gemini_job(cv_id="pk", application_email="careers@acme.pk", match_score=20))
        with mock.patch("core.pipeline.crawler.Crawler", return_value=fetcher):
            pipeline.run_crawl(run.pk)
        self.assertEqual(Application.objects.get().status, Status.SKIPPED)


class UkRuleTests(TwoCvTestCase):
    def test_remote_only_still_holds_when_the_uk_cv_is_chosen_with_both_ticked(self):
        self.gemini(gemini_job(cv_id="uk"), gemini_job(cv_id="pk"))
        app = self.submit(JD_TEXT + " This is a hybrid role.", [UK, PK])
        self.assertEqual(app.status, Status.SKIPPED)
        self.assertIn("hybrid role", app.note)
        self.assertEqual(mail.outbox, [])

    def test_uk_notes_are_unchanged(self):
        self.gemini(gemini_job(cv_id="uk", rules_met="no"))
        self.assertIn("Not fully remote", self.submit(JD_TEXT, [UK]).note)


class SendingTests(TwoCvTestCase):
    def test_sending_by_hand_attaches_the_cv_chosen_for_that_job(self):
        self.gemini(gemini_job(cv_id="pk", rules_met="no"))
        app = self.submit(LAHORE_JOB, [PK])
        self.assertEqual(app.status, Status.SKIPPED)
        pipeline.send_application(app.pk)
        self.assertEqual(mail.outbox[0].attachments[0][1], b"%PDF pakistan cv")
        self.assertEqual(mail.outbox[0].attachments[0][0], "Sam_Taylor_CV.pdf")

    def test_uploading_a_new_cv_doesnt_touch_the_other_one(self):
        with mock.patch("core.pipeline.resume.extract_text", return_value=PK_CV):
            pipeline.save_resume("new.pdf", b"%PDF new", PK)
        self.uk.refresh_from_db()
        self.assertEqual(bytes(self.uk.resume_bytes), b"%PDF-1.4 fake cv bytes")
        self.pk.refresh_from_db()
        self.assertEqual(bytes(self.pk.resume_bytes), b"%PDF new")

    def test_website_form_uses_the_chosen_cvs_details_and_facts(self):
        Profile.objects.filter(market="pk").update(facts="Where I live now: Lahore", allow_consent=True)
        app = Application.objects.create(
            source_hash="b" * 64, source="crawl", status=Status.FORM_QUEUE, job_url="https://acme.pk/apply", body="Dear Team, " + GOOD_BODY,
            jd_text=LAHORE_JOB, profile=Profile.load(PK),
        )
        crawler = mock.Mock()
        crawler.allowed.return_value = True
        with mock.patch("core.pipeline.crawler.Crawler", return_value=crawler):
            with mock.patch("core.pipeline.formfill.apply", return_value=formfill.Outcome(formfill.NEEDS_YOU)) as apply:
                pipeline._fill_form(app, mock.Mock())
        job = apply.call_args.args[1]
        self.assertEqual(job.resume_bytes, b"%PDF pakistan cv")
        self.assertEqual(job.phone, "+92 300 1234567")
        self.assertTrue(job.allow_consent)
        with mock.patch("core.llm.generate_json", return_value={"answers": []}) as generate:
            job.ask([formfill.Field("f1", "text", "text", "Where do you live?", True)])
        self.assertIn("Where I live now: Lahore", generate.call_args.args[0])
        self.assertIn("AI Engineer building LLM", generate.call_args.args[0])
        self.assertNotIn(CV_TEXT.strip()[:50], generate.call_args.args[0])


class CrawlTests(TwoCvTestCase):
    LISTING = "https://board.example/jobs"
    JOB = "https://board.example/jobs/1"

    def crawl(self, markets, label):
        page = Page(url=self.LISTING, text="Jobs", links=[(self.JOB, label)])
        job_page = Page(url=self.JOB, text=LAHORE_JOB, emails=["careers@acme.pk"])
        fetcher = mock.Mock()
        fetcher.fetch.side_effect = lambda url: page if url == self.LISTING else job_page
        fetcher.fetch_rendered.return_value = page
        run = CrawlRun.objects.create(url=self.LISTING, markets=markets)
        with mock.patch("core.pipeline.crawler.Crawler", return_value=fetcher):
            pipeline.run_crawl(run.pk)
        run.refresh_from_db()
        return run

    def test_job_list_is_judged_against_the_ticked_cvs(self):
        generate = self.gemini({"page_type": "listing", "job_links": [1]}, gemini_job(cv_id="pk", application_email="careers@acme.pk"))
        self.crawl([UK, PK], "AI Engineer - Lahore")
        triage = generate.call_args_list[0].args[0]
        self.assertIn("Lahore or Islamabad", triage)
        self.assertIn("fully remote jobs open", triage)
        app = Application.objects.get()
        self.assertEqual((app.profile, app.markets, app.status), (self.pk, [UK, PK], Status.SENT))

    def test_listing_labelled_hybrid_is_dropped_only_for_the_uk_cv(self):
        self.gemini({"page_type": "listing", "job_links": [1]}, {"page_type": "listing", "job_links": [1]})
        self.assertEqual(self.crawl([UK], "AI Engineer - Hybrid working").total, 0)
        self.gemini({"page_type": "listing", "job_links": [1]}, gemini_job(cv_id="pk", application_email="careers@acme.pk"))
        self.assertEqual(self.crawl([PK], "AI Engineer - Hybrid working").total, 1)


class DashboardTests(TwoCvTestCase):
    def apply(self, cvs, jd=LAHORE_JOB, action="send"):
        return self.client.post(reverse("apply_jd"), {"jd": jd, "action": action, "cv": cvs}, follow=True)

    def test_home_offers_both_cvs_and_the_choice_in_both_forms(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, 'name="resume"', count=2)
        self.assertContains(response, 'name="facts"', count=2)
        self.assertContains(response, 'name="cv"', count=4)
        self.assertContains(response, "With both ticked, it uses whichever CV suits the job best", count=2)
        self.assertContains(response, "Lahore or Islamabad only")

    def test_pakistan_facts_start_from_their_own_template(self):
        self.assertContains(self.client.get(reverse("home")), "Cities I&#x27;ll work in: Lahore or Islamabad only")

    def test_ticks_are_remembered(self):
        with mock.patch("core.llm.generate_json", return_value=gemini_job(cv_id="pk", application_email="careers@acme.pk")):
            self.apply(["pk"])
        self.assertEqual({p.market: p.selected for p in Profile.options()}, {"uk": False, "pk": True})
        self.assertContains(self.client.get(reverse("home")), 'value="pk" checked', count=2)

    def test_choice_reaches_the_application(self):
        with mock.patch("core.llm.generate_json", return_value=gemini_job(cv_id="pk", application_email="careers@acme.pk")):
            self.apply(["uk", "pk"])
        self.assertEqual(Application.objects.get().markets, ["uk", "pk"])

    def test_nothing_ticked_is_refused_and_the_text_kept(self):
        response = self.apply([])
        self.assertContains(response, "Tick at least one CV")
        self.assertContains(response, "AI Engineer, Lahore")
        self.assertEqual(Application.objects.count(), 0)

    def test_made_up_choices_are_ignored(self):
        self.assertContains(self.apply(["mars"]), "Tick at least one CV")

    def test_ticking_a_cv_that_isnt_uploaded_says_so(self):
        Profile.objects.filter(market="pk").update(resume_bytes=b"", resume_text="")
        response = self.apply(["pk"])
        self.assertContains(response, "First, upload your Pakistan CV")
        self.assertEqual(Application.objects.count(), 0)

    def test_crawl_records_the_choice(self):
        with mock.patch("core.pipeline.spawn"):
            self.client.post(reverse("start_crawl"), {"url": "https://board.example/jobs", "cv": ["uk", "pk"]})
        self.assertEqual(CrawlRun.objects.get().markets, ["uk", "pk"])

    def test_crawl_with_nothing_ticked_is_refused(self):
        response = self.client.post(reverse("start_crawl"), {"url": "https://board.example/jobs"}, follow=True)
        self.assertContains(response, "Tick at least one CV")
        self.assertContains(response, "https://board.example/jobs")
        self.assertEqual(CrawlRun.objects.count(), 0)

    def test_upload_details_and_facts_go_to_the_right_cv(self):
        with mock.patch("core.pipeline.resume.extract_text", return_value=PK_CV):
            self.client.post(reverse("upload_resume"), {"market": "pk", "resume": SimpleUploadedFile("pk.pdf", b"%PDF x")})
        self.client.post(reverse("save_profile"), {"market": "pk", "full_name": "Sam T", "phone": "+92 1", "linkedin": "l"})
        self.client.post(reverse("save_facts"), {"market": "pk", "facts": "Where I live now: Lahore", "allow_consent": "on"})
        self.pk.refresh_from_db()
        self.assertEqual((self.pk.resume_name, self.pk.full_name, self.pk.facts, self.pk.allow_consent), ("pk.pdf", "Sam T", "Where I live now: Lahore", True))
        self.uk.refresh_from_db()
        self.assertEqual((self.uk.full_name, self.uk.facts, self.uk.allow_consent), ("Sam Taylor", "", False))

    def test_untouched_pakistan_template_isnt_saved_as_facts(self):
        self.client.post(reverse("save_facts"), {"market": "pk", "facts": prompts.PAKISTAN_FACTS_TEMPLATE})
        self.pk.refresh_from_db()
        self.assertEqual(self.pk.facts, "")

    def test_made_up_market_is_treated_as_the_uk_cv(self):
        self.client.post(reverse("save_facts"), {"market": "mars", "facts": "x"})
        self.uk.refresh_from_db()
        self.assertEqual(self.uk.facts, "x")

    def test_pages_show_which_cv_was_used(self):
        app = Application.objects.create(source_hash="c" * 64, source="paste", status=Status.SENT, profile=self.pk, title="AI Engineer")
        self.assertContains(self.client.get(reverse("detail", args=[app.pk])), "Using your Pakistan CV")
        self.assertContains(self.client.get(reverse("activity")), "(Pakistan CV)")
