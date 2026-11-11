"""Who each CV says you are: country, email, links and name read from the CV, and several Gmail accounts to send from."""
import os
from unittest import mock

from django.core import mail
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.urls import reverse

from core import countries, emails, mailer, pipeline, resume
from core.models import Application, Profile, Resume
from core.tests.base import AppTestCase, minimal_pdf

UK_CV = (
    "Muhammad Hamza\nAI Engineer\nhamza@gmail.com\n+447412284417\nRomford, London RM6 4PS\ngithub.com/lord-hamza\n"
    "Profile\nAI Engineer with 7+ years in Python. I've worked with clients in the US, Canada and the EU.\n"
)
PK_CV = (
    "Muhammad Hamza\nPython/AI Engineer\nhamza@gmail.com\n92 333 0481822\nLahore\nlinkedin.com/in/mu-hamza\n"
    "Profile\nAI Engineer delivering for US, Canadian, and EU clients.\n"
)
US_CV = "Sam Taylor\nSenior AI Engineer | Austin, TX 78701 | (512) 555-0147 | sam@outlook.com\nSummary\nBuilt RAG systems.\n"


class CountryTests(AppTestCase):
    def test_places_by_name_city_postcode_and_state(self):
        cases = {
            "Romford, London RM6 4PS": "United Kingdom",
            "Lahore": "Pakistan",
            "Islamabad, Pakistan": "Pakistan",
            "Austin, TX 78701": "United States",
            "Toronto, ON M5V 3L9": "Canada",
            "Dubai, UAE": "United Arab Emirates",
            "Belfast, Northern Ireland": "United Kingdom",
            "Sydney, New South Wales": "Australia",
            "Atlanta, Georgia": "United States",  # Georgia alone could be the country or the state
            "Tbilisi, Georgia": "Georgia",
            "Remote (US)": "United States",
            "U.S.A.": "United States",
        }
        for text, country in cases.items():
            with self.subTest(text):
                self.assertEqual(countries.first_place(text), country)

    def test_words_that_only_look_like_places_dont_count(self):
        for text in ("we help us grow", "github.com/london-dev", "hamza@london.co.uk", "Nigerian", "Python3 4EE"):
            with self.subTest(text):
                self.assertEqual(countries.places(text), [])

    def test_phone_numbers(self):
        cases = {
            "+447412284417": ("United Kingdom", 2),
            "+92 333 0481822": ("Pakistan", 2),
            "0092 333 0481822": ("Pakistan", 2),
            "03330481822": ("Pakistan", 1),
            "07412 284417": ("United Kingdom", 1),
            "+1 (415) 555-0123": ("United States", 2),
            "+1 416 555 0123": ("Canada", 2),
            "(512) 555-0147": ("United States", 2),  # only North America writes numbers this way
            "1-416-555-0123": ("Canada", 2),
            "+971 50 123 4567": ("United Arab Emirates", 2),
            "+7 701 123 4567": ("Kazakhstan", 2),
            "92 333 0481822": ("", 0),  # no + and no leading 0: can't tell
            "": ("", 0),
        }
        for phone, expected in cases.items():
            with self.subTest(phone):
                self.assertEqual(countries.from_phone(phone), expected)

    def test_canonical_names(self):
        for typed, name in {"uk": "United Kingdom", "U.K.": "United Kingdom", "usa": "United States", "us": "United States",
                            "England": "United Kingdom", "pakistan": "Pakistan", " UAE ": "United Arab Emirates",
                            "Atlantis": "Atlantis", "": ""}.items():
            with self.subTest(typed):
                self.assertEqual(countries.canonical(typed), name)
        self.assertTrue(countries.same("UK", "united kingdom"))
        self.assertFalse(countries.same("", ""))


class CvCountryTests(AppTestCase):
    def test_the_users_two_cvs(self):
        self.assertEqual(resume.guess_country(UK_CV, "+447412284417", "Muhammad Hamza"), "United Kingdom")
        self.assertEqual(resume.guess_country(PK_CV, "+92 333 0481822", "Muhammad Hamza"), "Pakistan")
        self.assertEqual(resume.guess_country(PK_CV, "", "Muhammad Hamza"), "Pakistan", "Lahore on its own is enough")

    def test_a_us_cv(self):
        self.assertEqual(resume.guess_country(US_CV, "(512) 555-0147", "Sam Taylor"), "United States")

    def test_countries_in_sentences_dont_count(self):
        text = "Sam Taylor\nPython developer with clients in the US and UK, building tools across Europe.\nExperience\n"
        self.assertEqual(resume.guess_country(text, "", "Sam Taylor"), "")

    def test_where_you_live_outvotes_the_phone(self):
        self.assertEqual(resume.guess_country("Sam\nLahore, Pakistan\nSummary\n", "+44 7700 900123", "Sam"), "Pakistan")

    def test_two_places_in_the_header_are_settled_by_the_phone(self):
        text = "Sam\nLahore, Pakistan | London, UK\nSummary\n"
        self.assertEqual(resume.guess_country(text, "+44 7700 900123", "Sam"), "United Kingdom")
        self.assertEqual(resume.guess_country(text, "", "Sam"), "Pakistan", "without a phone, the first place named")

    def test_places_you_only_mention_dont_count(self):
        for line in ("ML Engineer at Texas Instruments", "MSc AI, Imperial College London", "Open to roles in London",
                     "Happy to relocate to Dubai"):
            with self.subTest(line):
                self.assertEqual(resume.guess_country(f"Ali Raza\n{line}\nSummary\n", "+92 300 1234567", "Ali Raza"), "Pakistan")

    def test_a_region_code_is_only_a_hint_the_phone_can_outweigh(self):
        cases = {"Coimbatore, TN | +91 98765 43210": "India", "Haifa, IL | +972 50 123 4567": "Israel",
                 "Austin, TX": "United States", "Palo Alto, CA 94301": "United States"}
        for line, country in cases.items():
            with self.subTest(line):
                self.assertEqual(resume.contact_details("cv.docx", b"", f"Sam Lee\n{line}\nSummary\nWork.\n")["country"], country)

    def test_a_city_after_your_name_on_the_same_line_still_counts(self):
        self.assertEqual(resume.guess_country("Muhammad Hamza Austin, TX\nSummary\n", "", "Muhammad Hamza Austin"), "United States")

    def test_a_local_number_alone_isnt_enough(self):
        self.assertEqual(resume.guess_country("Sam\nDeveloper\n07700 900123\nSummary\n", "07700 900123", "Sam"), "")

    def test_where_you_are_based_in_the_summary_counts(self):
        text = "Sam Taylor\nDeveloper\nSummary\nBackend developer based in Dubai, building payment systems.\n"
        self.assertEqual(resume.guess_country(text, "", "Sam Taylor"), "United Arab Emirates")

    def test_letters_after_your_name_arent_a_state(self):
        self.assertEqual(resume.guess_country("Jane Doe, MD\nPhysician\nSummary\n", "+92 300 1234567", "Jane Doe"), "Pakistan")
        self.assertEqual(resume.guess_country("Sam Taylor, MS\nEngineer\nSummary\n", "", "Sam Taylor"), "")

    def test_codes_that_could_be_degrees_or_countries_need_a_zip(self):
        self.assertEqual(countries.places("Baltimore, MD"), [])
        self.assertEqual(countries.first_place("Baltimore, MD 21201"), "United States")
        self.assertEqual(countries.first_place("Berlin, DE"), "Germany")  # Berlin, not Delaware

    def test_other_peoples_places_in_the_summary_dont_count(self):
        text = "Sam\nEngineer\nSummary\nI build tools for clients based in the UK and Europe.\n"
        self.assertEqual(resume.guess_country(text, "+1 646 555 0147", "Sam"), "United States")

    def test_a_place_you_were_based_in_doesnt_outvote_your_phone(self):
        text = "Sam\nEngineer\nSummary\nPreviously based in London, now working remotely.\n"
        self.assertEqual(resume.guess_country(text, "+1 646 555 0147", "Sam"), "United States")

    def test_a_job_location_naming_two_countries_names_none(self):
        self.assertEqual(countries.only_place("London, ON"), "")
        self.assertEqual(countries.only_place("Dubai / Lahore"), "")
        self.assertEqual(countries.only_place("Lahore, Pakistan"), "Pakistan")

    def test_your_name_is_never_read_as_a_place(self):
        self.assertEqual(resume.guess_country("India Smith\nDesigner\nSummary\n", "", "India Smith"), "")

    def test_header_keeps_short_pieces_that_could_say_where_you_are(self):
        self.assertEqual(resume.header(UK_CV, "Muhammad Hamza").splitlines(), ["Romford, London RM6 4PS"])


class CvDetailsTests(AppTestCase):
    def test_email_and_links_from_the_print_and_the_clickable_links(self):
        data = minimal_pdf(
            "x",
            links=["mailto:hamza@gmail.com", "tel:+447412284417", "https://github.com/lord-hamza", "https://hamza.dev/"],
        )
        found = resume.contact_details("cv.pdf", data, UK_CV)
        self.assertEqual(
            {key: found[key] for key in ("full_name", "email", "phone", "country")},
            {"full_name": "Muhammad Hamza", "email": "hamza@gmail.com", "phone": "+447412284417", "country": "United Kingdom"},
        )
        self.assertEqual(found["links"], "https://github.com/lord-hamza\nhttps://hamza.dev/")

    def test_email_only_behind_a_link(self):
        data = minimal_pdf("x", links=["mailto:Sam.Taylor@Example.org?subject=Hi"])
        self.assertEqual(resume.contact_details("cv.pdf", data, "Sam Taylor\nDeveloper\n" * 20)["email"], "sam.taylor@example.org")

    def test_the_header_email_beats_a_referees(self):
        text = "Sam Taylor\nsam@gmail.com\nDeveloper\n" + "Experience\nLots.\n" * 10 + "Referee: boss@acme.com\n"
        self.assertEqual(resume.contact_details("cv.docx", b"", text)["email"], "sam@gmail.com")

    def test_a_link_cut_off_by_a_line_break_gives_way_to_the_clickable_one(self):
        text = "Sam\ngithub.com/lord-\nhamza\nSummary\n"
        data = minimal_pdf("x", links=["https://github.com/lord-hamza"])
        self.assertEqual(resume.contact_details("cv.pdf", data, text)["links"], "https://github.com/lord-hamza")

    def test_profile_sites_need_a_path_or_your_own_subdomain(self):
        text = "Sam\nBuilt the x.com integration and a github.com app.\nSee lord-hamza.github.io and kaggle.com/sam.\n" * 3
        self.assertEqual(resume.contact_details("cv.docx", b"", text)["links"], "https://lord-hamza.github.io\nhttps://kaggle.com/sam")

    def test_linkedin_mail_and_phone_links_stay_out_of_the_links(self):
        data = minimal_pdf("x", links=["https://www.linkedin.com/in/sam", "mailto:a@b.com", "tel:+447700900123"])
        self.assertEqual(resume.contact_details("cv.pdf", data, "Sam\nDeveloper\n" * 30)["links"], "")

    def test_clean_links(self):
        typed = " github.com/sam \nhttps://github.com/sam/\nmailto:x@y.com\njavascript:alert(1)\nlinkedin.com/in/sam\nnot a link\nkaggle.com/sam,"
        self.assertEqual(resume.clean_links(typed), ["https://github.com/sam", "https://kaggle.com/sam"])
        self.assertEqual(len(resume.clean_links("\n".join(f"https://x{n}.dev" for n in range(50)))), resume.MAX_CV_LINKS)

    def test_a_us_cv_with_a_north_american_number(self):
        text = "Jane Doe\nSoftware Engineer | Palo Alto, CA | (650) 555-0123 | jane@gmail.com\nSummary\nBuilt things.\n"
        found = resume.contact_details("cv.docx", b"", text)
        self.assertEqual((found["phone"], found["country"], found["email"]), ("(650) 555-0123", "United States", "jane@gmail.com"))
        self.assertEqual(resume.contact_details("cv.docx", b"", "Ann Lee\n1-416-555-0123\nSummary\nWork.\n")["country"], "Canada")

    def test_nothing_after_a_references_heading_is_yours(self):
        for heading in ("References", "Professional References", "Referees"):
            text = ("Sam Taylor\nDeveloper\nSummary\n" + "Built Django APIs for years.\n" * 12
                    + f"{heading}\nDr Jane Smith, jane.smith@acme.co.uk, 0161 496 0000, linkedin.com/in/janesmith-data\n")
            data = minimal_pdf("x", links=["https://www.linkedin.com/in/sam-taylor", "mailto:sam@gmail.com"])
            with self.subTest(heading):
                found = resume.contact_details("cv.pdf", data, text)
                self.assertEqual((found["phone"], found["email"], found["linkedin"]),
                                 ("", "sam@gmail.com", "https://www.linkedin.com/in/sam-taylor"))

    def test_a_contact_table_at_the_top_of_a_word_file_comes_first(self):
        import io
        from docx import Document
        document = Document()
        document.add_paragraph("Sam Taylor")
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text, table.rows[0].cells[1].text = "+44 7700 900123", "Leeds, UK"
        for _ in range(15):
            document.add_paragraph("Built Django APIs and AWS pipelines for agencies.")
        buffer = io.BytesIO()
        document.save(buffer)
        text = resume.extract_text("cv.docx", buffer.getvalue())
        self.assertLess(text.index("+44 7700 900123"), text.index("Built Django"))

    def test_your_own_github_comes_before_one_your_cv_mentions(self):
        text = "Sam\nDeveloper\nSummary\nOpen-source contributor to github.com/huggingface and more.\n"
        data = minimal_pdf("x", links=["https://github.com/sam-t"])
        found = resume.contact_details("cv.pdf", data, text)
        self.assertEqual(resume.github_profile(found["links"]), "https://github.com/sam-t")

    def test_github_profile_not_a_repository(self):
        self.assertEqual(resume.github_profile("https://github.com/sam/project\nhttps://www.github.com/sam/"), "https://github.com/sam")
        self.assertEqual(resume.github_profile("https://github.com/sam/project"), "")
        self.assertEqual(resume.github_profile(""), "")
        self.assertEqual(resume.github_profile("https://github.com/lord-"), "", "a name can't end in a hyphen: it was cut off")


class MailboxTests(AppTestCase):
    def test_same_inbox_however_written(self):
        self.assertEqual(emails.mailbox("Sam.Taylor+jobs@GoogleMail.com"), "samtaylor@gmail.com")
        self.assertEqual(emails.mailbox("sam.taylor+jobs@acme.com"), "sam.taylor@acme.com", "dots matter outside Gmail")

    def test_own_addresses_never_found_as_recipients(self):
        text = "Send your CV to s.a.m.taylor@gmail.com or jobs@acme.com, or to pk@gmail.com."
        self.assertEqual(emails.find_emails(text, exclude=["samtaylor@gmail.com", "PK@gmail.com"]), ["jobs@acme.com"])
        self.assertEqual(emails.find_emails(text, exclude="samtaylor@gmail.com"), ["jobs@acme.com", "pk@gmail.com"])


class AccountsTests(AppTestCase):
    MORE = {
        "GMAIL_ADDRESS_2": "pk.hamza@gmail.com", "GMAIL_APP_PASSWORD_2": "pppp pppp pppp pppp",
        "GMAIL_ADDRESS_3": "half@gmail.com",  # no password yet: not connected
        "GMAIL_ADDRESS_4": "Sam.Taylor@gmail.com", "GMAIL_APP_PASSWORD_4": "dddd",  # the first account again
    }

    def test_every_connected_account_once_in_order(self):
        with mock.patch.dict(os.environ, self.MORE):
            self.assertEqual(mailer.accounts(), ["sam@gmail.com", "pk.hamza@gmail.com", "sam.taylor@gmail.com"])
            self.assertEqual(mailer.account("pkhamza+x@googlemail.com"), "pk.hamza@gmail.com")
            self.assertEqual(mailer.account("half@gmail.com"), "")
            self.assertEqual(mailer.password("pk.hamza@gmail.com"), "pppppppppppppppp")

    def test_where_a_new_account_goes(self):
        with mock.patch.dict(os.environ, self.MORE):
            self.assertEqual(mailer.slot_for("PK.Hamza@gmail.com"), 2, "the same mailbox keeps its slot")
            self.assertEqual(mailer.slot_for("half@gmail.com"), 3)
            self.assertEqual(mailer.slot_for("new@gmail.com"), 5)
        full = {f"GMAIL_ADDRESS_{n}": f"a{n}@gmail.com" for n in range(2, mailer.MAX_ACCOUNTS + 1)}
        with mock.patch.dict(os.environ, full):
            self.assertIsNone(mailer.slot_for("new@gmail.com"))

    def test_sends_from_the_account_asked_for(self):
        with mock.patch.dict(os.environ, self.MORE), mock.patch("core.mailer.get_connection") as connect:
            connect.return_value.send_messages.return_value = 1
            mailer.send("jobs@acme.com", "Hi", "Hello", "Muhammad Hamza", "cv.pdf", b"%PDF", "application/pdf",
                        sender="pkhamza@gmail.com")
        self.assertEqual((connect.call_args.kwargs["username"], connect.call_args.kwargs["password"]),
                         ("pk.hamza@gmail.com", "pppppppppppppppp"))

    def test_an_account_that_was_removed_cant_send(self):
        with self.assertRaisesMessage(mailer.MailError, "isn't connected any more"):
            mailer.send("jobs@acme.com", "Hi", "Hello", "Sam", "cv.pdf", b"%PDF", "application/pdf", sender="gone@gmail.com")
        self.assertEqual(mail.outbox, [])

    def test_without_a_sender_the_first_account_sends(self):
        mailer.send("jobs@acme.com", "Hi", "Hello", "Sam", "cv.pdf", b"%PDF", "application/pdf")
        self.assertEqual(mail.outbox[0].message()["From"], "Sam <sam@gmail.com>")


class SetupViewTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.make_profile()

    def save(self, **data):
        return self.save_setup(**data)

    def test_every_detail_is_saved_and_tidied(self):
        with mock.patch.dict(os.environ, {"GMAIL_ADDRESS_2": "sam.uk@gmail.com", "GMAIL_APP_PASSWORD_2": "x"}):
            self.save(email=" Sam.UK@Gmail.com ", links="github.com/sam\r\nhttps://github.com/sam/\r\nmailto:x@y.z",
                      country=" uk ", send_from="samuk@gmail.com")
        self.cv.refresh_from_db()
        self.assertEqual(
            (self.cv.email, self.cv.links, self.cv.country, self.cv.send_from),
            ("sam.uk@gmail.com", "https://github.com/sam", "United Kingdom", "sam.uk@gmail.com"),
        )

    def test_a_bad_email_or_unknown_account_keeps_the_old_value_and_saves_the_rest(self):
        Resume.objects.filter(pk=self.cv.pk).update(email="sam@gmail.com", send_from="sam@gmail.com")
        response = self.save(email="hamza@gmail", phone="1", send_from="stranger@gmail.com", facts="Salary: PKR 600,000")
        self.assertContains(response, "hamza@gmail isn&#x27;t a valid email address, so the old one stays")
        self.assertContains(response, "stranger@gmail.com isn&#x27;t a connected Gmail account")
        self.cv.refresh_from_db()
        self.assertEqual((self.cv.email, self.cv.send_from, self.cv.phone, self.cv.facts),
                         ("sam@gmail.com", "sam@gmail.com", "1", "Salary: PKR 600,000"))

    def test_a_chosen_account_survives_a_save_while_none_is_connected(self):
        Resume.objects.filter(pk=self.cv.pk).update(send_from="sam.uk@gmail.com")
        with mock.patch.dict(os.environ, {"GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": ""}):
            self.assertNotContains(self.client.get(reverse("home")), 'name="send_from"')
            self.save(phone="1")
        self.cv.refresh_from_db()
        self.assertEqual(self.cv.send_from, "sam.uk@gmail.com")

    def test_a_cv_left_without_an_account_opens_setup_and_says_so(self):
        with mock.patch.dict(os.environ, {"GMAIL_ADDRESS_2": "other@gmail.com", "GMAIL_APP_PASSWORD_2": "x"}):
            Resume.objects.filter(pk=self.cv.pk).update(email="sam@outlook.com")
            page = self.client.get(reverse("home"))
        self.assertTrue(page.context["setup_open"])
        self.assertContains(page, "Setup: choose a Gmail account for my cv final (2).pdf")
        self.assertContains(page, "No Gmail account sends its emails yet: connect sam@outlook.com")

    def test_two_cvs_sending_from_one_account_are_flagged(self):
        Resume.objects.filter(pk=self.cv.pk).update(email="sam.uk@outlook.com")
        Resume.objects.create(name="pk.pdf", data=b"%PDF", text="x", full_name="Sam", email="sam.pk@outlook.com")
        self.assertContains(self.client.get(reverse("home")), "Another CV sends from the same Gmail account", count=2)

    def test_a_country_you_type_stays_as_typed_if_unknown(self):
        self.save(country="Atlantis")
        self.cv.refresh_from_db()
        self.assertEqual(self.cv.country, "Atlantis")

    def test_the_card_shows_what_was_read_and_what_needs_you(self):
        Resume.objects.filter(pk=self.cv.pk).update(country="Canada", email="sam@gmail.com")
        Resume.objects.create(name="two.pdf", data=b"%PDF", text="x", email="Sam@gmail.com", country="Pakistan")
        Profile.objects.update(remote_countries="United Kingdom")
        page = self.client.get(reverse("home"))
        self.assertContains(page, "Canada isn't in your job rules")
        self.assertNotContains(page, "Pakistan isn't in your job rules", msg_prefix="Pakistan is an on-site place")
        self.assertContains(page, "Name not found")
        self.assertContains(page, "Another CV has the same email address", count=2)
        self.assertContains(page, "Automatic: sam@gmail.com")
        self.assertContains(page, '<option value="United Kingdom">')

    def test_the_rules_form_saves_the_cv_per_country_choice(self):
        self.save_setup(cv_per_country=False)
        self.assertFalse(Profile.load().cv_per_country)
        self.save_setup(cv_per_country=True)
        self.assertTrue(Profile.load().cv_per_country)


class GmailAccountViewTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.make_profile()

    def connect(self, address, password="abcdabcdabcdabcd"):
        with mock.patch("core.mailer.verify"):
            return self.client.post(reverse("connect_gmail"), {"address": address, "password": password}, follow=True)

    def test_three_accounts_then_the_same_one_again(self):
        self.connect("uk@gmail.com")
        self.connect("pk@gmail.com")
        self.connect("U.K@gmail.com", "zzzz zzzz zzzz zzzz")  # the same mailbox: it keeps its place, with the new password
        self.assertEqual(mailer.accounts(), ["sam@gmail.com", "u.k@gmail.com", "pk@gmail.com"])
        self.assertEqual(mailer.password("uk@gmail.com"), "zzzzzzzzzzzzzzzz")
        self.assertContains(self.client.get(reverse("home")), "3 Gmail accounts")

    def test_no_room_left(self):
        full = {f"GMAIL_ADDRESS_{n}": f"a{n}@gmail.com" for n in range(2, mailer.MAX_ACCOUNTS + 1)}
        with mock.patch.dict(os.environ, full):
            response = self.connect("new@gmail.com")
        self.assertContains(response, f"up to {mailer.MAX_ACCOUNTS} Gmail accounts")

    def test_removing_one(self):
        self.connect("uk@gmail.com")
        Resume.objects.filter(pk=self.cv.pk).update(send_from="uk@gmail.com")
        response = self.client.post(reverse("disconnect_gmail"), {"address": "uk@gmail.com"}, follow=True)
        self.assertContains(response, "Removed uk@gmail.com.")
        self.assertNotContains(response, "need an account", msg_prefix="your only account now sends everything")
        self.assertEqual(mailer.accounts(), ["sam@gmail.com"])
        self.assertIn("GMAIL_ADDRESS_2=''", self.env_file.read_text())
        self.assertEqual(pipeline.sender(Resume.objects.get(pk=self.cv.pk)), "sam@gmail.com")

    def test_removing_the_account_a_cv_used_while_others_remain(self):
        self.connect("uk@gmail.com")
        self.connect("pk@gmail.com")
        Resume.objects.filter(pk=self.cv.pk).update(email="uk@gmail.com")
        response = self.client.post(reverse("disconnect_gmail"), {"address": "uk@gmail.com"}, follow=True)
        self.assertContains(response, "my cv final (2).pdf now need an account to send from")
        self.assertContains(response, "No Gmail account sends its emails yet")

    def test_the_password_is_never_shown(self):
        self.connect("uk@gmail.com", "secretsecretsecr")
        self.assertNotContains(self.client.get(reverse("home")), "secretsecretsecr")


class DetailViewTests(AppTestCase):
    def test_the_same_job_with_other_cvs_and_where_it_went_from(self):
        self.make_profile()
        other = Resume.objects.create(name="us.pdf", data=b"%PDF", text="x", full_name="Sam Taylor")
        first = Application.objects.create(source_hash="a", source="paste", resume=self.cv, status="sent", body="Hi",
                                           recipient="jobs@acme.com", sender="sam@gmail.com", open_to="Open to people anywhere")
        Application.objects.create(source_hash="b", source="paste", resume=other, status="ready", body="Hi", parent=first)
        page = self.client.get(reverse("detail", args=[first.pk]))
        self.assertContains(page, "Open to people anywhere")
        self.assertContains(page, "from sam@gmail.com")
        self.assertContains(page, "The same job with your other CVs")
        self.assertContains(page, "us.pdf")


class IdentityMigrationTests(TransactionTestCase):
    """Your saved name moves to each CV, and each CV's email, links and country are read from it."""

    def migrate(self, setup):
        executor = MigrationExecutor(connection)
        executor.migrate([("core", "0005_cv_details")])
        setup(executor.loader.project_state([("core", "0005_cv_details")]).apps)
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_your_name_and_what_each_cv_says(self):
        def setup(old):
            old.get_model("core", "Profile").objects.create(full_name="Muhammad Hamza")
            OldResume = old.get_model("core", "Resume")
            OldResume.objects.create(name="uk.pdf", text=UK_CV, phone="+447412284417",
                                     data=minimal_pdf("x", links=["mailto:hamza@gmail.com", "https://github.com/lord-hamza"]))
            OldResume.objects.create(name="pk.pdf", text=PK_CV, phone="+92 333 0481822", data=b"not a pdf")

        self.migrate(setup)
        uk, pk = Resume.objects.order_by("id")
        self.assertEqual((uk.full_name, uk.email, uk.country, uk.links),
                         ("Muhammad Hamza", "hamza@gmail.com", "United Kingdom", "https://github.com/lord-hamza"))
        self.assertEqual((pk.full_name, pk.email, pk.country, pk.links), ("Muhammad Hamza", "hamza@gmail.com", "Pakistan", ""))
        self.assertTrue(Profile.load().cv_per_country)

    def test_without_a_saved_name_each_cv_says_its_own(self):
        def setup(old):
            old.get_model("core", "Profile").objects.create(full_name="")
            old.get_model("core", "Resume").objects.create(name="cv.pdf", text=US_CV, data=b"%PDF")

        self.migrate(setup)
        self.assertEqual((Resume.objects.get().full_name, Resume.objects.get().country), ("Sam Taylor", "United States"))

    def test_undoing_puts_your_name_back(self):
        def setup(old):
            old.get_model("core", "Profile").objects.create(full_name="Muhammad Hamza")
            old.get_model("core", "Resume").objects.create(name="cv.pdf", text=UK_CV, data=b"%PDF")

        self.migrate(setup)
        executor = MigrationExecutor(connection)
        try:
            executor.migrate([("core", "0005_cv_details")])
            Profile = executor.loader.project_state([("core", "0005_cv_details")]).apps.get_model("core", "Profile")
            self.assertEqual(Profile.objects.get().full_name, "Muhammad Hamza")
        finally:  # later tests expect the latest schema
            executor = MigrationExecutor(connection)
            executor.loader.build_graph()
            executor.migrate(executor.loader.graph.leaf_nodes())

    def test_no_cvs_is_fine(self):
        self.migrate(lambda old: old.get_model("core", "Profile").objects.create(full_name="Sam"))
        self.assertFalse(Resume.objects.exists())


class AddGmailTests(AppTestCase):
    def setUp(self):
        super().setUp()
        self.make_profile()

    def test_the_plus_opens_setup_at_the_form(self):
        home = self.client.get(reverse("home"))
        self.assertContains(home, 'href="/?add=gmail#add-gmail"')
        self.assertContains(home, 'aria-label="Add a Gmail account">+</a>')
        page = self.client.get(reverse("home"), {"add": "gmail"})
        self.assertTrue(page.context["setup_open"])
        self.assertContains(page, '<details id="add-gmail" class="add-account" open>')

    def test_no_plus_once_every_slot_is_used(self):
        full = {f"GMAIL_ADDRESS_{n}": f"a{n}@gmail.com" for n in range(2, mailer.MAX_ACCOUNTS + 1)}
        full.update({f"GMAIL_APP_PASSWORD_{n}": "x" for n in range(2, mailer.MAX_ACCOUNTS + 1)})
        with mock.patch.dict(os.environ, full):
            home = self.client.get(reverse("home"))
        self.assertNotContains(home, "add=gmail")
        self.assertNotContains(home, 'id="add-gmail"')

    def test_a_failed_login_comes_back_to_the_open_form(self):
        with mock.patch("core.mailer.verify", side_effect=mailer.MailError("Gmail rejected the login")):
            response = self.client.post(reverse("connect_gmail"), {"address": "new@gmail.com", "password": "bad"})
        self.assertEqual(response["Location"], "/?add=gmail#add-gmail")

    def test_without_any_account_the_form_is_already_open(self):
        with mock.patch.dict(os.environ, {"GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": ""}):
            page = self.client.get(reverse("home"))
        self.assertContains(page, '<details id="add-gmail" class="add-account" open>')
        self.assertContains(page, "Add a Gmail account</summary>")
