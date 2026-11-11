"""Many CVs: uploading them at once, which one each job gets, the details that go with each, and the job rules they all share."""
import re
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse

from core import pipeline, prompts
from core.models import Application, Profile, Resume
from core.tests.base import CV_TEXT, JD_TEXT, AppTestCase, gemini_job, minimal_pdf

Status = Application.Status
WP_CV = "Sam Taylor\nWordPress developer building themes, plugins and WooCommerce shops for agencies. " * 3
LAHORE_JOB = (
    "WordPress Developer, Lahore, Pakistan. This is a hybrid role, with 3 days a week in the office. "
    "You will build themes and plugins. To apply, send your CV to careers@acme.pk."
)
LONDON_HYBRID = JD_TEXT.replace("(Remote, UK)", "(London). This is a hybrid role")


def lahore_job(**overrides):
    """How Gemini reads LAHORE_JOB."""
    return gemini_job(
        **{"location": "Lahore, Pakistan", "work_setup": "hybrid", "limited_to": ["Pakistan"],
           "application_email": "careers@acme.pk", **overrides}
    )


class CvTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.make_profile()
        self.wp = Resume.objects.create(name="wordpress.pdf", data=b"%PDF wordpress cv", text=WP_CV, full_name="Sam Taylor")

    def gemini(self, *answers):
        patcher = mock.patch("core.llm.generate_json", side_effect=list(answers))
        self.addCleanup(patcher.stop)
        return patcher.start()

    def submit(self, text, auto_send=True):
        app, _ = pipeline.submit_jd(text, auto_send)
        pipeline.process(app.pk)
        app.refresh_from_db()
        return app


class ChoosingTests(CvTestCase):
    def test_every_cv_reaches_gemini_in_one_call(self):
        generate = self.gemini(gemini_job(cv_id=str(self.cv.pk)))
        self.submit(JD_TEXT)
        self.assertEqual(generate.call_count, 1)
        prompt = generate.call_args.args[0]
        for words in (CV_TEXT.strip()[:50], "WordPress developer building", f'cv_option id="{self.cv.pk}"', f'cv_option id="{self.wp.pk}"'):
            with self.subTest(words):
                self.assertIn(words, prompt)
        self.assertEqual(prompt.count("<facts>"), 2, "each CV brings its own facts")

    def test_the_cv_gemini_picks_is_attached(self):
        self.gemini(lahore_job(cv_id=str(self.wp.pk)))
        app = self.submit(LAHORE_JOB)
        self.assertEqual((app.resume, app.status), (self.wp, Status.SENT))
        self.assertEqual(mail.outbox[0].attachments[0][1], b"%PDF wordpress cv")

    def test_an_unknown_cv_id_is_written_again_with_the_best_scoring_cv_alone(self):
        scores = [{"cv_id": str(self.cv.pk), "match_score": 50}, {"cv_id": str(self.wp.pk), "match_score": 70}]
        generate = self.gemini(gemini_job(cv_id="nonsense", cv_scores=scores), gemini_job(cv_id=str(self.wp.pk)))
        self.assertEqual(self.submit(JD_TEXT).resume, self.wp)
        second = generate.call_args_list[1].args[0]
        self.assertIn(f'cv_option id="{self.wp.pk}"', second)
        self.assertNotIn(f'cv_option id="{self.cv.pk}"', second)

    def test_no_cv_fails_with_a_reason(self):
        Resume.objects.all().delete()
        app = self.submit(JD_TEXT)
        self.assertEqual(app.status, Status.FAILED)
        self.assertIn("Upload your CV", app.note)


class DetailsFollowTheCvTests(CvTestCase):
    """A CV's phone, LinkedIn and facts go out with the jobs that get it, never with another CV's jobs."""

    def setUp(self):
        super().setUp()
        Resume.objects.filter(pk=self.cv.pk).update(facts="Where I live now: Leeds, United Kingdom")
        Resume.objects.filter(pk=self.wp.pk).update(
            phone="+92 333 0000000", linkedin="", facts="Where I live now: Lahore, Pakistan"
        )

    def option(self, prompt, cv):
        return re.search(rf'<cv_option id="{cv.pk}"[^>]*>(.*?)</cv_option>', prompt, re.S).group(1)

    def test_each_cv_brings_its_own_facts_to_gemini(self):
        generate = self.gemini(lahore_job(cv_id=str(self.wp.pk)))
        self.submit(LAHORE_JOB)
        prompt = generate.call_args.args[0]
        uk, pk = self.option(prompt, self.cv), self.option(prompt, self.wp)
        self.assertIn("Leeds, United Kingdom", uk)
        self.assertNotIn("Lahore, Pakistan", uk)
        self.assertIn("Lahore, Pakistan", pk)
        self.assertNotIn("Leeds", pk)
        self.assertIn("ignore any other option's", prompts.TAILOR_SYSTEM)

    def test_cv_choice_starts_from_where_the_job_is_open_to(self):
        self.assertIn(
            "If limited_to isn't empty, choose among the options whose for_jobs_in is in it. Otherwise, unless the job "
            "is open to anyone anywhere, prefer an option whose for_jobs_in is based_in. Then choose the highest match_score.",
            prompts.TAILOR_SYSTEM,
        )

    def test_email_is_signed_with_the_chosen_cvs_details(self):
        self.gemini(lahore_job(cv_id=str(self.wp.pk)))
        body = self.submit(LAHORE_JOB).body
        self.assertTrue(body.endswith("Kind regards,\nSam Taylor\n+92 333 0000000"), body[-80:])
        self.assertNotIn("+44", body)
        self.assertNotIn("linkedin", body.lower())

    def test_the_other_cv_signs_with_its_own(self):
        self.gemini(gemini_job(cv_id=str(self.cv.pk)))
        body = self.submit(JD_TEXT).body
        self.assertTrue(body.endswith("Kind regards,\nSam Taylor\n+44 7700 900123 | linkedin.com/in/samtaylor"))
        self.assertNotIn("+92", body)

    def test_a_rewrite_for_quality_keeps_the_cv_and_shows_gemini_only_that_one(self):
        generate = self.gemini(gemini_job(cv_id=str(self.cv.pk), body="Hi, see CV."), gemini_job(cv_id=str(self.wp.pk)))
        app = self.submit(JD_TEXT, auto_send=False)
        self.assertEqual(app.resume, self.cv)
        self.assertTrue(app.body.endswith("+44 7700 900123 | linkedin.com/in/samtaylor"))
        self.assertNotIn(f'cv_option id="{self.wp.pk}"', generate.call_args_list[1].args[0])

    def test_crawl_triage_doesnt_need_facts(self):
        options = [pipeline._cv(cv, 50) for cv in (self.cv, self.wp)]
        prompt = prompts.triage_prompt("rules", options, "page", [], 5)
        self.assertNotIn("<facts>", prompt)
        self.assertNotIn("Lahore, Pakistan", prompt)


class JobRulesTests(CvTestCase):
    def test_hybrid_is_fine_in_a_listed_place(self):
        self.gemini(lahore_job(cv_id=str(self.wp.pk)))
        self.assertEqual(self.submit(LAHORE_JOB).status, Status.SENT)

    def test_hybrid_abroad_is_skipped_whatever_gemini_says(self):
        self.gemini(gemini_job(rules_met="yes", cv_id=str(self.cv.pk)))
        app = self.submit(LONDON_HYBRID)
        self.assertEqual(app.status, Status.SKIPPED)
        self.assertIn("hybrid role", app.note)

    def test_hybrid_at_home_is_skipped_once_you_take_the_place_off(self):
        Profile.objects.update(onsite_places="Islamabad")
        self.gemini(lahore_job(cv_id=str(self.wp.pk)))
        self.assertEqual(self.submit(LAHORE_JOB).status, Status.SKIPPED)

    def test_rules_follow_your_settings(self):
        Profile.objects.update(onsite_places="", remote_countries="")
        generate = self.gemini(gemini_job())
        self.submit(JD_TEXT)
        self.assertIn("Only fully remote jobs.", generate.call_args.args[0])

    def test_rules_form_saves_and_tidies(self):
        self.save_setup(onsite_places=" Karachi ,, Lahore", remote_countries="Canada")
        profile = Profile.load()
        self.assertEqual((profile.onsite_places, profile.remote_countries), ("Karachi, Lahore", "Canada"))
        self.assertContains(
            self.client.get(reverse("home")), "any work setup in Karachi, Lahore, and fully remote jobs open to people in Canada or anywhere"
        )


class UploadManyTests(CvTestCase):
    def upload(self, *names):
        files = [SimpleUploadedFile(name, b"%PDF " + name.encode()) for name in names]
        with mock.patch("core.pipeline.resume.extract_text", return_value=CV_TEXT):
            return self.client.post(reverse("upload_resume"), {"resume": files}, follow=True)

    def test_several_at_once(self):
        response = self.upload("bd.pdf", "seo.docx")
        self.assertContains(response, "2 CVs saved")
        self.assertEqual(Resume.objects.count(), 4)

    def test_same_name_replaces(self):
        self.assertContains(self.upload("wordpress.pdf"), "Replaced wordpress.pdf, keeping the facts you&#x27;d saved")
        self.assertEqual(bytes(Resume.objects.get(name="wordpress.pdf").data), b"%PDF wordpress.pdf")
        self.assertEqual(Resume.objects.count(), 2)

    def test_one_bad_file_doesnt_stop_the_rest(self):
        files = [SimpleUploadedFile("bad.exe", b"MZ"), SimpleUploadedFile("bd.pdf", b"%PDF bd")]
        with mock.patch("core.pipeline.resume.extract_text", side_effect=[pipeline.resume.ResumeError("nope"), CV_TEXT]):
            response = self.client.post(reverse("upload_resume"), {"resume": files}, follow=True)
        self.assertContains(response, "bad.exe: nope")
        self.assertTrue(Resume.objects.filter(name="bd.pdf").exists())

    def test_there_is_a_limit(self):
        for number in range(Resume.MAX_COUNT - 2):
            Resume.objects.create(name=f"{number}.pdf", data=b"x", text="x")
        self.assertContains(self.upload("one too many.pdf"), "up to 10 CVs")

    def test_remove(self):
        self.client.post(reverse("delete_resume", args=[self.wp.pk]))
        self.assertEqual(list(Resume.objects.all()), [self.cv])


class MoveOldCvsTests(TransactionTestCase):
    def test_both_old_cvs_carry_over(self):
        executor = MigrationExecutor(connection)
        executor.migrate([("core", "0003_two_cvs")])
        old = executor.loader.project_state([("core", "0003_two_cvs")]).apps
        OldProfile, OldApp = old.get_model("core", "Profile"), old.get_model("core", "Application")
        uk = OldProfile.objects.create(
            market="uk", full_name="Sam", resume_name="uk.pdf", resume_bytes=b"uk", resume_text="UK CV", facts="Leeds"
        )
        OldProfile.objects.create(market="pk", resume_name="pk.pdf", resume_bytes=b"pk", resume_text="PK CV", facts="Lahore")
        OldApp.objects.create(source_hash="a", source="paste", profile=uk)

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        self.assertEqual(list(Resume.objects.values_list("name", flat=True)), ["uk.pdf", "pk.pdf"])
        self.assertEqual(Application.objects.get().resume.name, "uk.pdf")
        self.assertEqual(list(Resume.objects.values_list("full_name", flat=True)), ["Sam", "Sam"])
        self.assertEqual(list(Resume.objects.values_list("facts", flat=True)), ["Leeds", ""])


class CvDetailsMigrationTests(TransactionTestCase):
    """Details you saved once for all CVs move to the CVs themselves."""

    def migrate(self, setup):
        executor = MigrationExecutor(connection)
        executor.migrate([("core", "0004_many_cvs")])
        setup(executor.loader.project_state([("core", "0004_many_cvs")]).apps)
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_each_cv_gets_what_it_says_and_your_facts_go_to_the_first(self):
        def setup(old):
            old.get_model("core", "Profile").objects.create(
                full_name="Sam", phone="+44 7700 900123", facts="Where I live now: Leeds"
            )
            OldResume = old.get_model("core", "Resume")
            OldResume.objects.create(name="uk.pdf", data=minimal_pdf("x"), text="Sam Taylor\n+44 7700 900123\nDeveloper")
            OldResume.objects.create(
                name="pk.pdf",
                data=minimal_pdf("x", links=["tel:92 333 0000000", "https://www.linkedin.com/in/sam-pk"]),
                text="Sam Taylor\nAI engineer",
            )
            OldResume.objects.create(name="broken.pdf", data=b"not a pdf", text="Sam Taylor\nWordPress")

        self.migrate(setup)
        uk, pk, broken = Resume.objects.order_by("id")
        self.assertEqual((uk.phone, uk.linkedin, uk.facts), ("+44 7700 900123", "", "Where I live now: Leeds"))
        self.assertEqual((pk.phone, pk.linkedin, pk.facts), ("+92 333 0000000", "https://www.linkedin.com/in/sam-pk", ""))
        self.assertEqual((broken.phone, broken.linkedin, broken.facts), ("", "", ""))
        self.assertEqual([uk.full_name, pk.full_name, broken.full_name], ["Sam", "Sam", "Sam"])

    def test_a_detail_you_typed_that_no_cv_shows_stays_with_the_cvs_that_lack_one(self):
        def setup(old):
            old.get_model("core", "Profile").objects.create(
                full_name="Sam", phone="+44 7700 900123", linkedin="linkedin.com/in/sam"
            )
            OldResume = old.get_model("core", "Resume")
            OldResume.objects.create(name="uk.pdf", data=minimal_pdf("x"), text="Sam\n+44 7700 900123")
            OldResume.objects.create(
                name="pk.pdf",
                data=minimal_pdf("x", links=["tel:92 333 0000000", "https://www.linkedin.com/in/sam-pk"]),
                text="Sam\nAI",
            )
            OldResume.objects.create(name="wp.pdf", data=minimal_pdf("x"), text="Sam\nWordPress")

        self.migrate(setup)
        self.assertEqual(
            list(Resume.objects.order_by("id").values_list("phone", "linkedin")),
            [
                ("+44 7700 900123", "linkedin.com/in/sam"),
                ("+92 333 0000000", "https://www.linkedin.com/in/sam-pk"),
                ("", "linkedin.com/in/sam"),
            ],
        )

    def test_with_one_cv_the_details_you_saved_win(self):
        def setup(old):
            old.get_model("core", "Profile").objects.create(
                full_name="Sam", phone="07000 000000", linkedin="linkedin.com/in/typed", facts="x"
            )
            old.get_model("core", "Resume").objects.create(name="cv.pdf", data=b"%PDF", text="Sam\n+44 7700 900123")

        self.migrate(setup)
        cv = Resume.objects.get()
        self.assertEqual((cv.phone, cv.linkedin, cv.facts), ("07000 000000", "linkedin.com/in/typed", "x"))

    def test_unsent_drafts_are_signed_with_their_own_cvs_details(self):
        old_signature = "\n\nKind regards,\nSam\n+44 7700 900123"

        def setup(old):
            old.get_model("core", "Profile").objects.create(full_name="Sam", phone="+44 7700 900123")
            OldResume, OldApp = old.get_model("core", "Resume"), old.get_model("core", "Application")
            uk = OldResume.objects.create(name="uk.pdf", data=minimal_pdf("x"), text="Sam\n+44 7700 900123")
            pk = OldResume.objects.create(name="pk.pdf", data=minimal_pdf("x", links=["tel:92 333 0000000"]), text="Sam\nAI")
            for digest, status, cv in (("1", "ready", pk), ("2", "sent", pk), ("3", "skipped", uk)):
                OldApp.objects.create(source_hash=digest, source="paste", status=status, resume=cv, body=f"Hello.{old_signature}")

        self.migrate(setup)
        bodies = list(Application.objects.order_by("source_hash").values_list("body", flat=True))
        self.assertEqual(bodies, ["Hello.\n\nKind regards,\nSam\n+92 333 0000000", f"Hello.{old_signature}", f"Hello.{old_signature}"])

    def test_undoing_puts_your_details_back(self):
        def setup(old):
            old.get_model("core", "Profile").objects.create(full_name="Sam", phone="07000 000000", facts="Where I live now: Leeds")
            old.get_model("core", "Resume").objects.create(name="cv.pdf", data=b"%PDF", text="Sam")

        self.migrate(setup)
        executor = MigrationExecutor(connection)
        try:
            executor.migrate([("core", "0004_many_cvs")])
            profile = executor.loader.project_state([("core", "0004_many_cvs")]).apps.get_model("core", "Profile").objects.get()
            self.assertEqual((profile.phone, profile.facts), ("07000 000000", "Where I live now: Leeds"))
        finally:  # later tests expect the latest schema
            executor = MigrationExecutor(connection)
            executor.loader.build_graph()
            executor.migrate(executor.loader.graph.leaf_nodes())

    def test_with_one_cv_what_the_app_filled_in_is_read_again(self):
        def setup(old):
            old.get_model("core", "Profile").objects.create(
                full_name="Sam", phone="07700 900123", linkedin="linkedin.com/in/muhammad-hamza-"
            )
            old.get_model("core", "Resume").objects.create(
                name="cv.pdf",
                data=minimal_pdf("x", links=["tel:+447700900123", "https://www.linkedin.com/in/muhammad-hamza-a1b2c3d4"]),
                text="Sam\n07700 900123\nlinkedin.com/in/muhammad-hamza-\na1b2c3d4",
            )

        self.migrate(setup)
        cv = Resume.objects.get()
        self.assertEqual((cv.phone, cv.linkedin), ("+447700900123", "https://www.linkedin.com/in/muhammad-hamza-a1b2c3d4"))

    def test_rows_that_may_have_gone_out_keep_their_signature(self):
        signed = "Hello.\n\nKind regards,\nSam\n+44 7700 900123"
        notes = [
            "Interrupted while sending. Check your Gmail Sent folder before sending again.",
            pipeline.INTERRUPTED_FORM,
            *pipeline.formfill.MAYBE_SUBMITTED,
        ]
        self.assertIn(pipeline.INTERRUPTED_SEND, notes)

        def setup(old):
            old.get_model("core", "Profile").objects.create(full_name="Sam", phone="+44 7700 900123")
            OldResume, OldApp = old.get_model("core", "Resume"), old.get_model("core", "Application")
            OldResume.objects.create(name="uk.pdf", data=minimal_pdf("x"), text="Sam\n+44 7700 900123")
            pk = OldResume.objects.create(name="pk.pdf", data=minimal_pdf("x", links=["tel:92 333 0000000"]), text="Sam\nAI")
            for number, note in enumerate(notes):
                OldApp.objects.create(source_hash=str(number), source="paste", status="needs_you", resume=pk, body=signed, note=note)

        self.migrate(setup)
        self.assertEqual(set(Application.objects.values_list("body", flat=True)), {signed})

    def test_no_cvs_is_fine(self):
        self.migrate(lambda old: old.get_model("core", "Profile").objects.create(full_name="Sam", facts="x"))
        self.assertFalse(Resume.objects.exists())
