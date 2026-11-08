"""Many CVs: uploading them at once, which one each job gets, and the job rules they all share."""
from unittest import mock

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse

from core import pipeline
from core.models import Application, Profile, Resume
from core.tests.base import CV_TEXT, JD_TEXT, AppTestCase, gemini_job

Status = Application.Status
WP_CV = "Sam Taylor\nWordPress developer building themes, plugins and WooCommerce shops for agencies. " * 3
LAHORE_JOB = (
    "WordPress Developer, Lahore, Pakistan. This is a hybrid role, with 3 days a week in the office. "
    "You will build themes and plugins. To apply, send your CV to careers@acme.pk."
)
LONDON_HYBRID = JD_TEXT.replace("(Remote, UK)", "(London). This is a hybrid role")


class CvTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.make_profile()
        self.wp = Resume.objects.create(name="wordpress.pdf", data=b"%PDF wordpress cv", text=WP_CV)

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
        self.assertEqual(prompt.count("<facts>"), 1, "your facts are shared, not repeated per CV")

    def test_the_cv_gemini_picks_is_attached(self):
        self.gemini(gemini_job(cv_id=str(self.wp.pk), application_email="careers@acme.pk"))
        app = self.submit(LAHORE_JOB)
        self.assertEqual((app.resume, app.status), (self.wp, Status.SENT))
        self.assertEqual(mail.outbox[0].attachments[0][1], b"%PDF wordpress cv")

    def test_an_unknown_cv_id_falls_back_to_the_first_cv(self):
        self.gemini(gemini_job(cv_id="nonsense"))
        self.assertEqual(self.submit(JD_TEXT).resume, self.cv)

    def test_no_cv_fails_with_a_reason(self):
        Resume.objects.all().delete()
        app = self.submit(JD_TEXT)
        self.assertEqual(app.status, Status.FAILED)
        self.assertIn("Upload your CV", app.note)


class JobRulesTests(CvTestCase):
    def test_hybrid_is_fine_in_a_listed_place(self):
        self.gemini(gemini_job(cv_id=str(self.wp.pk), application_email="careers@acme.pk"))
        self.assertEqual(self.submit(LAHORE_JOB).status, Status.SENT)

    def test_hybrid_abroad_is_skipped_whatever_gemini_says(self):
        self.gemini(gemini_job(rules_met="yes"))
        app = self.submit(LONDON_HYBRID)
        self.assertEqual(app.status, Status.SKIPPED)
        self.assertIn("hybrid role", app.note)

    def test_hybrid_at_home_is_skipped_once_you_take_the_place_off(self):
        Profile.objects.update(onsite_places="Islamabad")
        self.gemini(gemini_job(cv_id=str(self.wp.pk), application_email="careers@acme.pk"))
        self.assertEqual(self.submit(LAHORE_JOB).status, Status.SKIPPED)

    def test_rules_follow_your_settings(self):
        Profile.objects.update(onsite_places="", remote_countries="")
        generate = self.gemini(gemini_job())
        self.submit(JD_TEXT)
        self.assertIn("Only fully remote jobs.", generate.call_args.args[0])

    def test_rules_form_saves_and_tidies(self):
        self.client.post(reverse("save_rules"), {"onsite_places": " Karachi ,, Lahore", "remote_countries": "Canada"})
        profile = Profile.load()
        self.assertEqual((profile.onsite_places, profile.remote_countries), ("Karachi, Lahore", "Canada"))
        self.assertContains(self.client.get(reverse("home")), "any work setup in Karachi, Lahore, and fully remote jobs from Canada")


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
        self.upload("wordpress.pdf")
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
        uk = OldProfile.objects.create(market="uk", full_name="Sam", resume_name="uk.pdf", resume_bytes=b"uk", resume_text="UK CV")
        OldProfile.objects.create(market="pk", resume_name="pk.pdf", resume_bytes=b"pk", resume_text="PK CV", facts="Lahore")
        OldApp.objects.create(source_hash="a", source="paste", profile=uk)

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        self.assertEqual(list(Resume.objects.values_list("name", flat=True)), ["uk.pdf", "pk.pdf"])
        self.assertEqual(Application.objects.get().resume.name, "uk.pdf")
        profile = Profile.objects.get()
        self.assertEqual((profile.full_name, profile.facts), ("Sam", "Lahore"))
