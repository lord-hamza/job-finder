"""Which CV each job gets, by the countries it's open to, and a separate application per country for jobs open to several."""
import os
import re
from unittest import mock

from django.core import mail
from django.urls import reverse

from core import pipeline
from core.models import Application, Profile, Resume
from core.tests.base import AppTestCase, CV_TEXT, gemini_job

Status = Application.Status
WORLDWIDE = (
    "Senior AI Engineer, fully remote, worldwide. Acme hires from any country. Build RAG systems in Python. "
    "Send your CV to careers@acme.io."
)
UK_ONLY = "AI Engineer (Remote, UK). You must live in the UK. Build LLM tools in Python. Send your CV to jobs@acme.co.uk."
US_ONLY = "ML Engineer, Remote (US residents only). Python and PyTorch. Send your CV to talent@acme.com."
LAHORE_ONSITE = "AI Engineer, on-site in Lahore, Pakistan. Five days in our office. Send your CV to hr@acme.pk."
GOOD_REWRITE = "Written again. " + " ".join(["My Django and AWS work matches what your platform team needs."] * 5)


class RoutingTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        self._start(mock.patch.dict(os.environ, {
            "GMAIL_ADDRESS_2": "hamza.uk@gmail.com", "GMAIL_APP_PASSWORD_2": "uuuu",
            "GMAIL_ADDRESS_3": "hamza.pk@gmail.com", "GMAIL_APP_PASSWORD_3": "pppp",
            "GMAIL_ADDRESS_4": "hamza.us@gmail.com", "GMAIL_APP_PASSWORD_4": "ssss",
        }))
        Profile.load()
        self.uk = self.cv_for("uk.pdf", "United Kingdom", "hamza.uk@gmail.com", "+44 7412 284417")
        self.pk = self.cv_for("pk.pdf", "Pakistan", "hamza.pk@gmail.com", "+92 333 0481822")
        self.us = self.cv_for("us.pdf", "United States", "hamza.us@gmail.com", "+1 512 555 0147")

    def cv_for(self, name, country, email, phone):
        return Resume.objects.create(
            name=name, data=f"%PDF {name}".encode(), text=CV_TEXT, full_name="Muhammad Hamza",
            country=country, email=email, phone=phone,
        )

    def gemini(self, *answers):
        patcher = mock.patch("core.llm.generate_json", side_effect=list(answers))
        self.addCleanup(patcher.stop)
        return patcher.start()

    def scores(self, **by_name):
        return [{"cv_id": str(getattr(self, name).pk), "match_score": score} for name, score in by_name.items()]

    def answer(self, cv=None, **overrides):
        fields = {"work_setup": "remote", "limited_to": [], "based_in": "", "open_to_anywhere": False, "cv_scores": []}
        if cv is not None:
            fields["cv_id"] = str(cv.pk)
        fields.update(overrides)
        return gemini_job(**fields)

    def submit(self, text, auto_send=True):
        app, _ = pipeline.submit_jd(text, auto_send)
        pipeline.process(app.pk)
        app.refresh_from_db()
        return app

    def options(self, generate, call):
        return re.findall(r'<cv_option id="(\d+)"', generate.call_args_list[call].args[0])

    def sent_from(self):
        return [re.search(r"<(.*)>", message.message()["From"]).group(1) for message in mail.outbox]


class OneCountryTests(RoutingTestCase):
    def test_a_uk_only_job_gets_the_uk_cv_even_if_gemini_picks_another(self):
        generate = self.gemini(
            self.answer(self.pk, limited_to=["United Kingdom"], cv_scores=self.scores(uk=80, pk=88, us=70),
                        application_email="jobs@acme.co.uk"),
            self.answer(self.uk, application_email="", rules_met="no", is_job_posting=False, body=GOOD_REWRITE),
        )
        app = self.submit(UK_ONLY)
        self.assertEqual((app.resume, app.status, app.open_to), (self.uk, Status.SENT, "Open to people in United Kingdom"))
        self.assertEqual(self.options(generate, 1), [str(self.uk.pk)], "the email is written again from the UK CV alone")
        self.assertTrue(app.body.startswith("Written again"), "the rewrite gives the email, the first reading the rest")
        self.assertEqual(self.sent_from(), ["hamza.uk@gmail.com"])
        self.assertIn("+44 7412 284417", app.body)
        self.assertEqual(Application.objects.count(), 1)

    def test_a_us_only_job_gets_the_us_cv(self):
        generate = self.gemini(
            self.answer(self.us, limited_to=["USA"], cv_scores=self.scores(uk=80, pk=80, us=79), application_email="talent@acme.com")
        )
        app = self.submit(US_ONLY)
        self.assertEqual((app.resume, app.status), (self.us, Status.SENT))
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(self.sent_from(), ["hamza.us@gmail.com"])

    def test_the_location_decides_when_gemini_names_no_country(self):
        self.gemini(self.answer(self.uk, location="Remote (US)", cv_scores=self.scores(uk=90, us=60)),
                    self.answer(self.us, application_email="talent@acme.com"))
        self.assertEqual(self.submit(US_ONLY).resume, self.us)

    def test_a_job_only_open_to_a_country_without_a_cv_waits_for_you(self):
        self.us.delete()
        self.gemini(self.answer(self.uk, limited_to=["United States"], application_email="talent@acme.com"))
        app = self.submit(US_ONLY)
        self.assertEqual(app.status, Status.READY)
        self.assertFalse(app.auto_send)
        self.assertIn("only open to people in United States, and none of your CVs is for there", app.note)
        self.assertEqual(mail.outbox, [])

    def test_a_cv_without_a_country_only_stands_in_when_none_has_one(self):
        Resume.objects.filter(pk=self.uk.pk).update(country="")
        self.us.delete()
        self.gemini(self.answer(self.uk, limited_to=["United States"], application_email="talent@acme.com"))
        self.assertEqual(self.submit(US_ONLY).status, Status.READY, "the Pakistan CV has a country, so they're sorted")
        Resume.objects.update(country="")
        self.gemini(self.answer(self.uk, limited_to=["United States"], application_email="talent@acme.com"))
        self.assertEqual(self.submit(US_ONLY + " Again.").status, Status.SENT)

    def test_a_us_employer_with_no_stated_limit_gets_the_us_cv(self):
        self.gemini(self.answer(self.uk, based_in="United States", cv_scores=self.scores(uk=90, us=70)),
                    self.answer(self.us, application_email="talent@acme.com"))
        self.assertEqual(self.submit(US_ONLY.replace(" (US residents only)", "")).resume, self.us)

    def test_an_employer_in_a_country_without_a_cv_gets_the_best_match(self):
        self.gemini(self.answer(self.uk, based_in="United Arab Emirates", location="Dubai (Remote)",
                                cv_scores=self.scores(uk=90, pk=80, us=70), application_email="talent@acme.com"))
        app = self.submit(US_ONLY.replace(" (US residents only)", ", Dubai-based fintech"))
        self.assertEqual((app.resume, app.status, app.open_to), (self.uk, Status.SENT, "Remote, with an employer in United Arab Emirates"))

    def test_a_job_limited_to_two_of_your_countries_gets_the_better_cv_only(self):
        self.gemini(self.answer(self.uk, limited_to=["United Kingdom", "United States"], cv_scores=self.scores(uk=80, us=75, pk=99),
                                application_email="careers@acme.io"))
        app = self.submit(WORLDWIDE.replace("worldwide. Acme hires from any country", "UK or US residents"))
        self.assertEqual((app.resume, app.extras.count(), app.open_to), (self.uk, 0, "Open to people in United Kingdom, United States"))


class InPersonTests(RoutingTestCase):
    def test_an_on_site_job_in_lahore_only_ever_gets_the_pakistan_cv(self):
        generate = self.gemini(
            self.answer(self.uk, work_setup="on-site", location="Lahore, Pakistan", open_to_anywhere=True,
                        cv_scores=self.scores(uk=95, pk=70, us=90), application_email="hr@acme.pk"),
            self.answer(self.pk, application_email="hr@acme.pk"),
        )
        app = self.submit(LAHORE_ONSITE)
        self.assertEqual((app.resume, app.status), (self.pk, Status.SENT))
        self.assertEqual(self.options(generate, 1), [str(self.pk.pk)])
        self.assertEqual(Application.objects.count(), 1, "an on-site job never gets a CV per country")
        self.assertEqual(self.sent_from(), ["hamza.pk@gmail.com"])

    def test_an_unclear_setup_counts_as_in_person(self):
        self.gemini(self.answer(self.pk, work_setup="unclear", location="Karachi", open_to_anywhere=True,
                                cv_scores=self.scores(pk=80, uk=80, us=80), application_email="hr@acme.pk"))
        app = self.submit(LAHORE_ONSITE.replace("on-site in Lahore", "based in Karachi"))
        self.assertEqual((app.resume, Application.objects.count()), (self.pk, 1))

    def test_office_wording_counts_as_in_person_whatever_gemini_says(self):
        self.gemini(self.answer(self.pk, location="Islamabad", open_to_anywhere=True, application_email="hr@acme.pk"))
        app = self.submit("AI Engineer in Islamabad. This is a hybrid role, two days a week in the office. Email hr@acme.pk with your CV.")
        self.assertEqual((app.resume, Application.objects.count()), (self.pk, 1))

    def test_with_no_location_the_places_in_your_rules_decide(self):
        self.gemini(self.answer(self.uk, work_setup="on-site", location="", cv_scores=self.scores(uk=90)),
                    self.answer(self.pk, application_email="hr@acme.pk"))
        self.assertEqual(self.submit(LAHORE_ONSITE.replace("Lahore, Pakistan", "our Islamabad office")).resume, self.pk)

    def test_an_in_person_job_abroad_is_turned_down_even_if_it_names_your_city(self):
        text = "AI Engineer. Hybrid, London, 3 days in the office. We also have a team in Lahore. Email jobs@acme.co.uk."
        self.gemini(self.answer(self.uk, work_setup="hybrid", location="London", rules_met="yes", application_email="jobs@acme.co.uk"))
        app = self.submit(text)
        self.assertEqual(app.status, Status.SKIPPED)
        self.assertIn("isn't fully remote and is in United Kingdom", app.note)
        self.assertEqual(mail.outbox, [])


class WorldwideMixin:
    def worldwide(self, primary=None, **scores):
        scores = scores or {"pk": 82, "uk": 78, "us": 65}
        return self.answer(primary or self.pk, open_to_anywhere=True, cv_scores=self.scores(**scores),
                           application_email="careers@acme.io", location="Remote, worldwide")

    def further(self, cv, **overrides):
        """How Gemini reads the worldwide job again, with only one CV in front of it."""
        return self.answer(cv, **{"open_to_anywhere": True, "location": "Remote, worldwide",
                                  "application_email": "careers@acme.io", **overrides})


class EachCountryTests(WorldwideMixin, RoutingTestCase):
    def test_a_worldwide_job_gets_the_best_cv_of_each_country(self):
        generate = self.gemini(self.worldwide(), self.further(self.uk),
                               self.further(self.us))
        app = self.submit(WORLDWIDE)
        self.assertEqual((app.resume, app.status, app.open_to), (self.pk, Status.SENT, "Open to people anywhere"))
        extras = list(app.extras.order_by("pk"))
        self.assertEqual([(extra.resume, extra.status) for extra in extras], [(self.uk, Status.SENT), (self.us, Status.SENT)])
        self.assertEqual([self.options(generate, call) for call in (1, 2)], [[str(self.uk.pk)], [str(self.us.pk)]])
        self.assertEqual(self.sent_from(), ["hamza.pk@gmail.com", "hamza.uk@gmail.com", "hamza.us@gmail.com"])
        bodies = [message.body for message in mail.outbox]
        for body, phone in zip(bodies, ("+92 333 0481822", "+44 7412 284417", "+1 512 555 0147")):
            self.assertTrue(body.endswith(f"Muhammad Hamza\n{phone}"), body[-60:])
        self.assertEqual({extra.open_to for extra in extras}, {"Open to people anywhere"})

    def test_a_cv_below_your_minimum_doesnt_go(self):
        self.gemini(self.worldwide(pk=82, uk=78, us=59), self.further(self.uk))
        app = self.submit(WORLDWIDE)
        self.assertEqual([extra.resume for extra in app.extras.all()], [self.uk])

    def test_a_further_cv_that_scores_low_on_its_own_reading_is_skipped(self):
        self.gemini(self.worldwide(pk=82, uk=78, us=10), self.further(self.uk, match_score=40))
        extra = self.submit(WORLDWIDE).extras.get()
        self.assertEqual(extra.status, Status.SKIPPED)
        self.assertIn("below your minimum", extra.note)

    def test_a_further_cv_the_job_no_longer_reads_as_open_to_is_skipped(self):
        self.gemini(self.worldwide(pk=82, uk=78, us=10), self.further(self.uk, limited_to=["Pakistan"], open_to_anywhere=False))
        extra = self.submit(WORLDWIDE).extras.get()
        self.assertEqual(extra.status, Status.SKIPPED)
        self.assertIn("Read again, this job only takes one CV, so the one for United Kingdom wasn't sent.", extra.note)

    def test_cvs_with_one_email_send_once_and_the_other_is_noted(self):
        Resume.objects.filter(pk=self.uk.pk).update(email="hamza.pk@gmail.com")
        generate = self.gemini(self.worldwide(), self.further(self.us))
        app = self.submit(WORLDWIDE)
        noted = app.extras.get(resume=self.uk)
        self.assertEqual((noted.status, noted.auto_send, noted.score), (Status.SKIPPED, False, 78))
        self.assertIn("same email address as pk.pdf", noted.note)
        self.assertEqual(app.extras.get(resume=self.us).status, Status.SENT)
        self.assertEqual(generate.call_count, 2, "no Gemini call for a CV that won't be sent")
        self.assertEqual(len(mail.outbox), 2)

    def test_one_gmail_for_every_cv_sends_once(self):
        for slot in (2, 3, 4):
            os.environ[f"GMAIL_ADDRESS_{slot}"] = ""
        self.gemini(self.worldwide())
        app = self.submit(WORLDWIDE)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(set(app.extras.values_list("status", flat=True)), {Status.SKIPPED})
        self.assertIn("it would send from the same Gmail account as pk.pdf", app.extras.first().note)

    def test_a_crawled_job_waiting_for_a_cv_says_so_even_without_auto_apply(self):
        self.us.delete()
        app, _ = pipeline._claim("crawled", source=Application.Source.CRAWL, job_url="https://acme.com/jobs/1",
                                 jd_text=US_ONLY.replace("Send your CV to talent@acme.com.", ""), candidates=[], auto_send=False)
        self.gemini(self.answer(self.uk, limited_to=["United States"], application_email=""))
        pipeline.process(app.pk)
        app.refresh_from_db()
        self.assertEqual(app.status, Status.MANUAL)
        self.assertTrue(app.note.startswith("This job is only open to people in United States, and none of your CVs"), app.note)

    def test_turned_off_only_the_best_goes(self):
        Profile.objects.update(cv_per_country=False)
        self.gemini(self.worldwide())
        self.assertFalse(self.submit(WORLDWIDE).extras.exists())
        self.assertEqual(len(mail.outbox), 1)

    def test_two_cvs_for_one_country_send_only_the_better(self):
        uk2 = self.cv_for("uk2.pdf", "United Kingdom", "hamza.uk2@gmail.com", "+44 7700 900999")
        scores = [{"cv_id": str(uk2.pk), "match_score": 90}]
        self.gemini(
            self.answer(self.pk, open_to_anywhere=True, cv_scores=self.scores(pk=95, uk=80, us=10) + scores,
                        application_email="careers@acme.io"),
            self.further(uk2),
        )
        app = self.submit(WORLDWIDE)
        self.assertEqual([extra.resume for extra in app.extras.all()], [uk2])

    def test_a_further_cv_is_checked_against_all_your_cvs_like_the_first(self):
        reread = self.further(self.uk, based_in="Pakistan")  # an employer in Pakistan: that country's CV only
        self.gemini(self.worldwide(pk=82, uk=78, us=10), reread)
        extra = self.submit(WORLDWIDE).extras.get()
        self.assertEqual(extra.status, Status.SKIPPED)
        self.assertIn("only takes one CV", extra.note)

    def test_a_worldwide_job_from_an_employer_in_pakistan_only_gets_the_pakistan_cv(self):
        generate = self.gemini(
            self.worldwide(self.uk, uk=90, pk=80, us=85) | {"based_in": "Pakistan", "location": "Lahore (Remote)"},
            self.further(self.pk),
        )
        app = self.submit(WORLDWIDE.replace("Acme hires", "Acme, a Lahore company, hires"))
        self.assertEqual((app.resume, app.extras.count(), app.open_to), (self.pk, 0, "Open to people anywhere, with an employer in Pakistan"))
        self.assertEqual(self.options(generate, 1), [str(self.pk.pk)])

    def test_a_skipped_job_gets_no_further_cvs(self):
        self.gemini(self.worldwide() | {"rules_met": "no"})
        app = self.submit(WORLDWIDE)
        self.assertEqual(app.status, Status.SKIPPED)
        self.assertFalse(app.extras.exists())

    def test_a_draft_held_for_quality_doesnt_hold_back_the_other_cvs(self):
        bad = self.worldwide() | {"body": "Dear [Hiring Manager], " + "I fit well. " * 30}
        self.gemini(bad, self.answer(self.pk, body=bad["body"]), self.further(self.uk),
                    self.further(self.us))
        app = self.submit(WORLDWIDE)
        self.assertEqual((app.status, app.auto_send), (Status.READY, False))
        self.assertEqual(sorted(app.extras.values_list("status", flat=True)), [Status.SENT, Status.SENT])

    def test_drafts_stay_drafts(self):
        self.gemini(self.worldwide(), self.further(self.uk),
                    self.further(self.us))
        app = self.submit(WORLDWIDE, auto_send=False)
        self.assertEqual(set(Application.objects.values_list("status", flat=True)), {Status.READY})
        self.assertEqual(mail.outbox, [])
        self.assertEqual(app.extras.count(), 2)

    def test_the_daily_limit_counts_every_cv(self):
        self.gemini(self.worldwide(), self.further(self.uk),
                    self.further(self.us))
        with mock.patch.dict(os.environ, {"DAILY_SEND_LIMIT": "2"}):
            app = self.submit(WORLDWIDE)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("Daily limit", app.extras.get(resume=self.us).note)

    def test_a_crawled_job_without_an_email_queues_a_form_per_cv(self):
        app, _ = pipeline._claim("crawled", source=Application.Source.CRAWL, job_url="https://acme.io/jobs/1",
                                 jd_text=WORLDWIDE.replace("Send your CV to careers@acme.io.", ""), candidates=[], auto_send=True)
        self.gemini(self.worldwide() | {"application_email": ""}, self.further(self.uk, application_email=""),
                    self.further(self.us, application_email=""))
        pipeline.process(app.pk)
        self.assertEqual(
            sorted(Application.objects.values_list("resume__name", "status")),
            [("pk.pdf", Status.FORM_QUEUE), ("uk.pdf", Status.FORM_QUEUE), ("us.pdf", Status.FORM_QUEUE)],
        )


class GroupTests(WorldwideMixin, RoutingTestCase):
    def sent_group(self):
        self.gemini(self.worldwide(), self.further(self.uk),
                    self.further(self.us))
        return self.submit(WORLDWIDE)

    def test_retrying_the_first_never_doubles_a_cv(self):
        app = self.sent_group()
        Application.objects.filter(pk=app.pk).update(status=Status.QUEUED)  # as Retry does
        generate = self.gemini(self.answer(self.pk, open_to_anywhere=True, cv_scores=self.scores(pk=82),
                                           application_email="careers@acme.io"))
        pipeline.retry(app.pk)
        self.assertEqual(self.options(generate, 0), [str(self.pk.pk)], "the UK and US CVs already have their own")
        self.assertEqual(Application.objects.count(), 3)

    def test_retrying_a_further_cv_keeps_it(self):
        app = self.sent_group()
        extra = app.extras.get(resume=self.uk)
        Application.objects.filter(pk=extra.pk).update(status=Status.QUEUED)
        generate = self.gemini(self.answer(self.pk, open_to_anywhere=True, application_email="careers@acme.io"))
        pipeline.retry(extra.pk)
        extra.refresh_from_db()
        self.assertEqual((extra.resume, self.options(generate, 0)), (self.uk, [str(self.uk.pk)]))

    def test_the_same_job_posted_again_isnt_applied_to_again(self):
        self.sent_group()
        generate = self.gemini(self.worldwide())
        again = self.submit(WORLDWIDE + " Reposted.")
        self.assertEqual(again.status, Status.SKIPPED)
        self.assertIn("You already applied", again.note)
        self.assertFalse(again.extras.exists())
        self.assertEqual(generate.call_count, 1)

    def test_a_failed_further_cv_keeps_its_cv_from_the_first_and_tries_again_itself(self):
        app = self.sent_group()
        extra = app.extras.get(resume=self.us)
        Application.objects.filter(pk=extra.pk).update(status=Status.FAILED, note="Gmail said no")
        Application.objects.filter(pk=app.pk).update(status=Status.QUEUED)
        generate = self.gemini(self.answer(self.pk, open_to_anywhere=True, cv_scores=self.scores(pk=60),
                                           application_email="careers@acme.io"))
        pipeline.retry(app.pk)
        self.assertEqual(self.options(generate, 0), [str(self.pk.pk)], "the US CV stays with its own application")
        Application.objects.filter(pk=extra.pk).update(status=Status.QUEUED)
        self.gemini(self.further(self.us))
        pipeline.retry(extra.pk)
        extra.refresh_from_db()
        self.assertEqual((extra.resume, extra.status), (self.us, Status.SENT))
        self.assertEqual(Application.objects.count(), 3)

    def test_a_further_cv_whose_file_was_removed_fails_with_a_reason(self):
        app = self.sent_group()
        extra = app.extras.get(resume=self.us)
        Application.objects.filter(pk=extra.pk).update(status=Status.QUEUED)
        self.us.delete()
        self.gemini()
        pipeline.retry(extra.pk)
        extra.refresh_from_db()
        self.assertEqual(extra.status, Status.FAILED)
        self.assertIn("removed", extra.note)


class SenderTests(RoutingTestCase):
    def test_the_account_with_the_cvs_own_address(self):
        Resume.objects.filter(pk=self.uk.pk).update(email="Hamza.U.K+jobs@googlemail.com")
        self.uk.refresh_from_db()
        self.assertEqual(pipeline.sender(self.uk), "hamza.uk@gmail.com")

    def test_the_account_you_chose_wins(self):
        self.uk.send_from = "hamza.us@gmail.com"
        self.assertEqual(pipeline.sender(self.uk), "hamza.us@gmail.com")

    def test_with_several_accounts_none_is_borrowed(self):
        self.uk.email = "hamza@outlook.com"
        self.assertEqual(pipeline.sender(self.uk), "")
        self.uk.send_from = "removed@gmail.com"
        self.assertEqual(pipeline.sender(self.uk), "")
        with mock.patch.dict(os.environ, {f"GMAIL_ADDRESS_{slot}": "" for slot in (2, 3, 4)}):
            self.assertEqual(pipeline.sender(self.uk), "sam@gmail.com", "your only account sends everything")

    def test_a_cv_without_an_account_waits_with_a_reason(self):
        Resume.objects.filter(pk=self.uk.pk).update(email="hamza@outlook.com")
        self.gemini(self.answer(self.uk, limited_to=["United Kingdom"], application_email="jobs@acme.co.uk"))
        app = self.submit(UK_ONLY)
        self.assertEqual(app.status, Status.READY)
        self.assertIn("Connect hamza@outlook.com, or choose which Gmail account sends uk.pdf's emails", app.note)
        self.assertEqual(mail.outbox, [])

    def test_the_account_used_is_recorded(self):
        self.gemini(self.answer(self.uk, limited_to=["United Kingdom"], application_email="jobs@acme.co.uk"))
        app = self.submit(UK_ONLY)
        self.assertEqual(app.sender, "hamza.uk@gmail.com")
        self.assertEqual(mail.outbox[0].message()["From"], "Muhammad Hamza <hamza.uk@gmail.com>")
        self.assertEqual(mail.outbox[0].attachments[0][0], "Muhammad_Hamza_CV.pdf")

    def test_your_own_addresses_are_never_recipients(self):
        app, _ = pipeline.submit_jd(UK_ONLY + " Questions: hamza.pk@gmail.com or s.a.m@gmail.com.", True)
        self.assertEqual(app.candidates, ["jobs@acme.co.uk"])


class NameTests(RoutingTestCase):
    def test_a_cv_without_a_name_holds_setup(self):
        Resume.objects.filter(pk=self.us.pk).update(full_name="")
        self.assertEqual(pipeline.missing_setup(), ["add your name to us.pdf"])

    def test_a_cv_without_a_name_cant_send(self):
        Resume.objects.filter(pk=self.uk.pk).update(full_name="")
        self.gemini(self.answer(self.uk, limited_to=["United Kingdom"], application_email="jobs@acme.co.uk"))
        app = self.submit(UK_ONLY)
        self.assertEqual(app.status, Status.READY)
        self.assertIn("Add your name to uk.pdf", app.note)
        self.assertEqual(mail.outbox, [])

    def test_a_new_name_re_signs_drafts_and_their_subjects(self):
        body = "Hello.\n\nKind regards,\nMuhammad Hamza\n+44 7412 284417"
        mine = Application.objects.create(source_hash="1", source="paste", resume=self.uk, status=Status.READY,
                                          subject="Application for AI Engineer - Muhammad Hamza", body=body)
        signed_otherwise = Application.objects.create(source_hash="2", source="paste", resume=self.uk, status=Status.READY,
                                                      subject="Re: AI Engineer", body=body.replace("Muhammad Hamza", "Hamza"))
        self.uk.full_name = "M. Hamza"
        self.uk.save()
        self.assertEqual(pipeline.resign_drafts(self.uk, "Muhammad Hamza", self.uk.phone, ""), 1)
        mine.refresh_from_db()
        signed_otherwise.refresh_from_db()
        self.assertEqual(mine.subject, "Application for AI Engineer - M. Hamza")
        self.assertTrue(mine.body.endswith("Kind regards,\nM. Hamza\n+44 7412 284417"), mine.body)
        self.assertTrue(signed_otherwise.body.endswith("Hamza\n+44 7412 284417"))
        self.assertEqual(signed_otherwise.subject, "Re: AI Engineer")


class PromptTests(RoutingTestCase):
    def test_each_cv_brings_its_name_and_country_and_says_what_the_country_means(self):
        generate = self.gemini(self.answer(self.uk, limited_to=["United Kingdom"], application_email="jobs@acme.co.uk"))
        self.submit(UK_ONLY)
        prompt = generate.call_args.args[0]
        self.assertIn(f'<cv_option id="{self.uk.pk}" name="Muhammad Hamza" for_jobs_in="United Kingdom">', prompt)
        self.assertIn("<cv_countries>\nUnited Kingdom, Pakistan, United States\n</cv_countries>", prompt)
        self.assertIn("never where the candidate lives or may work", pipeline.prompts.TAILOR_SYSTEM)
        self.assertIn('Just "Remote" isn\'t enough.', pipeline.prompts.TAILOR_SYSTEM)

    def test_forms_for_the_us_cv_are_written_in_us_english(self):
        self.us.links = "https://github.com/hamza"
        with mock.patch("core.llm.generate_json", return_value={"answers": []}) as generate:
            pipeline._ask_gemini(Application(jd_text="Job"), self.us, [])
        self.assertIn("Spelling: US English", generate.call_args.args[0])
        with mock.patch("core.llm.generate_json", return_value={"answers": []}) as generate:
            pipeline._ask_gemini(Application(jd_text="Job"), self.uk, [])
        self.assertIn("Spelling: UK English", generate.call_args.args[0])


class SafeguardTests(WorldwideMixin, RoutingTestCase):
    def test_further_cvs_go_to_the_first_ones_address_not_their_own(self):
        self.gemini(self.worldwide(), self.further(self.uk, application_email="other@acme.io"), self.further(self.us))
        app = self.submit(WORLDWIDE + " Questions to other@acme.io.")
        self.assertEqual(set(Application.objects.values_list("recipient", flat=True)), {"careers@acme.io"})
        self.assertEqual({message.to[0] for message in mail.outbox}, {"careers@acme.io"})
        self.assertEqual(app.extras.count(), 2)

    def test_when_the_first_waits_for_you_so_do_the_others(self):
        self.gemini(self.worldwide() | {"application_email": ""}, self.further(self.uk), self.further(self.us))
        app = self.submit(WORLDWIDE)
        self.assertEqual(app.note, pipeline.UNCONFIRMED_ADDRESS)
        for extra in app.extras.all():
            self.assertEqual((extra.status, extra.auto_send, extra.recipient), (Status.READY, False, "careers@acme.io"))
            self.assertIn("waits for you, so this one does too", extra.note)
        self.assertEqual(mail.outbox, [])

    def test_nothing_goes_twice_from_one_address_even_after_accounts_change(self):
        app = self.submit_group_with_us_waiting()
        os.environ["GMAIL_ADDRESS_4"] = ""  # the US account is removed, and two remain...
        os.environ["GMAIL_ADDRESS_2"] = os.environ["GMAIL_ADDRESS_3"] = ""  # ...then only the first, which the PK CV used
        Resume.objects.filter(pk=self.pk.pk).update(email="sam@gmail.com")
        Application.objects.filter(pk=app.pk).update(sender="sam@gmail.com")
        us = app.extras.get(resume=self.us)
        pipeline.send_application(us.pk)
        us.refresh_from_db()
        self.assertEqual(us.status, Status.READY)
        self.assertEqual(us.note, pipeline.SAME_JOB_ALREADY)
        pipeline.claim_for_sending(us.pk)
        pipeline.deliver(us.pk, by_hand=True)  # you pressed Send: it goes
        us.refresh_from_db()
        self.assertEqual(us.status, Status.SENT)

    def submit_group_with_us_waiting(self):
        self.gemini(self.worldwide(), self.further(self.uk), self.further(self.us))
        with mock.patch.dict(os.environ, {"DAILY_SEND_LIMIT": "2"}):
            app = self.submit(WORLDWIDE)
        self.assertEqual(app.extras.get(resume=self.us).status, Status.READY)
        return app

    def test_a_job_read_as_not_for_you_isnt_written_again(self):
        generate = self.gemini(self.answer(self.pk, limited_to=["United Kingdom"], rules_met="no"))
        app = self.submit(UK_ONLY)
        self.assertEqual((app.status, generate.call_count), (Status.SKIPPED, 1))

    def test_a_website_form_already_sent_for_the_same_job_isnt_sent_again(self):
        Application.objects.create(source_hash="old", source=Application.Source.CRAWL, status=Status.SENT, title="Senior Python Developer",
                                   company="Acme Ltd", channel="form", sent_at=pipeline.timezone.now(), resume=self.uk)
        app, _ = pipeline._claim("new", source=Application.Source.CRAWL, job_url="https://jobs.acme.io/2", auto_send=True,
                                 jd_text=UK_ONLY.replace("Send your CV to jobs@acme.co.uk.", ""), candidates=[])
        self.gemini(self.answer(self.uk, limited_to=["United Kingdom"], application_email=""))
        pipeline.process(app.pk)
        app.refresh_from_db()
        self.assertEqual(app.status, Status.SKIPPED)
        self.assertIn("You already applied to Acme Ltd for this role", app.note)

    def test_a_form_for_a_cv_whose_address_already_went_isnt_submitted(self):
        app = self.submit_group_with_us_waiting()
        us = app.extras.get(resume=self.us)
        Resume.objects.filter(pk=self.us.pk).update(email="hamza.pk@gmail.com")
        Application.objects.filter(pk=us.pk).update(status=Status.FORM_QUEUE, recipient="", job_url="https://jobs.acme.io/1")
        us.refresh_from_db()
        with mock.patch("core.pipeline.formfill.apply") as apply:
            pipeline._fill_form(us, mock.Mock())
        apply.assert_not_called()
        us.refresh_from_db()
        self.assertEqual(us.status, Status.NEEDS_YOU)
        self.assertIn("already went out", us.note)


class ByHandTests(WorldwideMixin, RoutingTestCase):
    def test_pressing_send_yourself_goes_even_after_the_same_address_went(self):
        self.gemini(self.worldwide(), self.further(self.uk), self.further(self.us))
        with mock.patch.dict(os.environ, {"DAILY_SEND_LIMIT": "2"}):
            app = self.submit(WORLDWIDE)
        us = app.extras.get(resume=self.us)
        Resume.objects.filter(pk=self.us.pk).update(email="hamza.pk@gmail.com")  # now shares the first one's address
        response = self.client.post(reverse("send", args=[us.pk]),
                                    {"recipient": us.recipient, "subject": us.subject, "body": us.body}, follow=True)
        us.refresh_from_db()
        self.assertEqual((response.status_code, us.status), (200, Status.SENT))

    def test_a_further_application_whose_cv_was_removed_says_so(self):
        self.gemini(self.worldwide(), self.further(self.uk), self.further(self.us))
        with mock.patch.dict(os.environ, {"DAILY_SEND_LIMIT": "2"}):
            app = self.submit(WORLDWIDE)
        us = app.extras.get(resume=self.us)
        self.us.delete()
        pipeline.send_application(us.pk)
        us.refresh_from_db()
        self.assertEqual(us.status, Status.READY)
        self.assertIn("The CV for this application was removed, so it can't be sent", us.note)


class GeminiWordingTests(WorldwideMixin, RoutingTestCase):
    """Gemini doesn't always write a bare country name."""

    def test_countries_written_with_extra_words(self):
        cases = [
            (dict(work_setup="on-site", limited_to=["Lahore, Pakistan"]), self.pk, "In person in Pakistan"),
            (dict(based_in="the UK"), self.uk, "Remote, with an employer in United Kingdom"),
            (dict(limited_to=["UK residents only"]), self.uk, "Open to people in United Kingdom"),
            (dict(limited_to=["United States or Canada"]), self.us, "Open to people in United States, Canada"),
        ]
        for fields, cv, label in cases:
            with self.subTest(fields):
                reach = pipeline._reach(self.answer(**fields), [self.uk, self.pk, self.us], "A job.", Profile.load().onsite_places)
                self.assertEqual(([c.pk for c in reach["allowed"]], reach["label"], reach["refuse"], reach["hold"]),
                                 ([cv.pk], label, "", ""))

    def test_worldwide_written_as_a_limit_is_no_limit(self):
        reach = pipeline._reach(self.answer(open_to_anywhere=True, limited_to=["Worldwide"]), [self.uk, self.pk, self.us],
                                "Remote, worldwide.", "")
        self.assertEqual((reach["several"], reach["label"], reach["hold"]), (True, "Open to people anywhere", ""))

    def test_a_region_none_of_your_cvs_is_in_waits_for_you(self):
        self.us.delete()
        reach = pipeline._reach(self.answer(limited_to=["Americas"], based_in="United States"), [self.uk, self.pk],
                                "Remote (Americas only).", "")
        self.assertIn("only open to people in Americas, and none of your CVs is for there", reach["hold"])

    def test_an_employer_in_pakistan_written_with_its_city(self):
        reach = pipeline._reach(self.answer(open_to_anywhere=True, based_in="Lahore, Pakistan"), [self.uk, self.pk, self.us],
                                "Remote, work from anywhere.", Profile.load().onsite_places)
        self.assertEqual(([cv.pk for cv in reach["allowed"]], reach["several"]), ([self.pk.pk], False))

    def test_work_setup_is_one_of_four(self):
        self.assertEqual(pipeline.prompts.TAILOR_SCHEMA["properties"]["work_setup"]["enum"], ["remote", "hybrid", "on-site", "unclear"])


class HeldBackTests(WorldwideMixin, RoutingTestCase):
    def test_a_further_cv_that_was_skipped_doesnt_hold_its_address(self):
        Resume.objects.filter(pk=self.pk.pk).update(email="hamza.uk@gmail.com")  # the UK and PK CVs share an address
        self.gemini(self.worldwide(self.us, us=85, uk=80, pk=75), self.further(self.uk, match_score=40), self.further(self.pk))
        app = self.submit(WORLDWIDE)
        self.assertEqual(app.resume, self.us)
        self.assertEqual(dict(app.extras.values_list("resume__name", "status")), {"uk.pdf": Status.SKIPPED, "pk.pdf": Status.SENT})

    def test_the_next_cv_of_a_country_goes_when_the_best_shares_an_address(self):
        uk2 = self.cv_for("uk2.pdf", "United Kingdom", "hamza.uk2@gmail.com", "+44 7700 900999")
        os.environ["GMAIL_ADDRESS_5"], os.environ["GMAIL_APP_PASSWORD_5"] = "hamza.uk2@gmail.com", "x"
        Resume.objects.filter(pk=self.uk.pk).update(email="hamza.pk@gmail.com")  # shares the first one's address
        scores = self.scores(pk=90, uk=85, us=10) + [{"cv_id": str(uk2.pk), "match_score": 70}]
        self.gemini(self.answer(self.pk, open_to_anywhere=True, cv_scores=scores, application_email="careers@acme.io"),
                    self.further(uk2))
        app = self.submit(WORLDWIDE)
        self.assertEqual([(extra.resume, extra.status) for extra in app.extras.all()], [(uk2, Status.SENT)])

    def test_the_same_title_at_one_company_open_to_another_country_is_another_job(self):
        Application.objects.create(source_hash="lahore", source="paste", status=Status.SENT, title="Senior Python Developer",
                                   recipient="careers@acme.co.uk", open_to="In person in Pakistan", sent_at=pipeline.timezone.now())
        self.gemini(self.answer(self.uk, limited_to=["United Kingdom"]))
        self.assertEqual(self.submit(UK_ONLY.replace("jobs@acme.co.uk", "careers@acme.co.uk")).status, Status.SENT)

    def test_a_cv_removed_while_the_job_is_read_doesnt_stop_anything(self):
        def read(prompt, *args, **kwargs):
            if read.calls == 0:
                Resume.objects.filter(pk=self.us.pk).delete()
            read.calls += 1
            return [self.worldwide(), self.further(self.uk)][read.calls - 1]
        read.calls = 0
        with mock.patch("core.llm.generate_json", side_effect=read):
            app = self.submit(WORLDWIDE)
        self.assertEqual((app.status, [extra.resume for extra in app.extras.all()]), (Status.SENT, [self.uk]))


class IntegrityTests(WorldwideMixin, RoutingTestCase):
    def test_a_draft_written_before_the_cv_had_a_name_gets_it(self):
        Resume.objects.filter(pk=self.uk.pk).update(full_name="")
        self.gemini(self.answer(self.uk, limited_to=["United Kingdom"], application_email="jobs@acme.co.uk", subject=""))
        app = self.submit(UK_ONLY)
        self.assertTrue(app.body.endswith("Kind regards,\n+44 7412 284417"), app.body[-40:])
        self.save_setup(self.uk, full_name="Muhammad Hamza")
        app.refresh_from_db()
        self.assertTrue(app.body.endswith("Kind regards,\nMuhammad Hamza\n+44 7412 284417"), app.body[-60:])
        self.assertEqual(app.subject, "Application for Senior Python Developer - Muhammad Hamza")

    def test_a_form_already_filled_in_keeps_its_message(self):
        signed = "Hello.\n\nKind regards,\nMuhammad Hamza\n+44 7412 284417"
        filled = Application.objects.create(source_hash="f", source="crawl", resume=self.uk, status=Status.NEEDS_YOU, body=signed,
                                            form_answers=[{"label": "Name", "answer": "Muhammad Hamza", "source": "contact"}])
        self.assertEqual(pipeline.resign_drafts(self.uk, "Muhammad Hamza", "+44 7700 000000", ""), 0)
        filled.refresh_from_db()
        self.assertEqual(filled.body, signed)

    def noted_group(self):
        Resume.objects.filter(pk=self.uk.pk).update(email="hamza.pk@gmail.com")
        self.gemini(self.worldwide(pk=82, uk=78, us=10))
        app = self.submit(WORLDWIDE, auto_send=False)
        noted = app.extras.get(resume=self.uk)
        self.assertEqual(noted.status, Status.SKIPPED)
        return app, noted

    def test_a_noted_cv_doesnt_reserve_itself(self):
        app, noted = self.noted_group()
        self.pk.delete()
        Application.objects.filter(pk=app.pk).update(status=Status.QUEUED)
        self.gemini(self.answer(self.uk, open_to_anywhere=True, cv_scores=self.scores(uk=78), application_email="careers@acme.io"))
        pipeline.retry(app.pk)
        app.refresh_from_db()
        noted.refresh_from_db()
        self.assertEqual((app.resume, app.status), (self.uk, Status.READY))
        self.assertEqual(noted.note, "The first application for this job now uses this CV, so this one isn't needed.")
        Application.objects.filter(pk=noted.pk).update(status=Status.QUEUED)
        pipeline.retry(noted.pk)
        noted.refresh_from_db()
        self.assertEqual(noted.status, Status.FAILED, "it never drafts a second application with the same CV")

    def test_a_further_cv_redrafted_after_the_first_went_says_so(self):
        Resume.objects.filter(pk=self.uk.pk).update(email="hamza.pk@gmail.com")
        self.gemini(self.worldwide(pk=82, uk=78, us=10))
        app = self.submit(WORLDWIDE)
        noted = app.extras.get(resume=self.uk)
        Resume.objects.filter(pk=self.uk.pk).update(email="hamza.uk@gmail.com")  # given its own address, then Retry
        Application.objects.filter(pk=noted.pk).update(status=Status.QUEUED)
        self.gemini(self.further(self.uk))
        pipeline.retry(noted.pk)
        noted.refresh_from_db()
        self.assertEqual((noted.status, noted.note), (Status.READY, "Drafted for you to check. Click Send when you're happy with it."))
