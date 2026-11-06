import time

from django.test import SimpleTestCase

from core import emails


class FindEmailsTests(SimpleTestCase):
    def assertFinds(self, text, *expected):
        self.assertEqual(emails.find_emails(text), list(expected))

    def test_plain_address(self):
        self.assertFinds("Send your CV to careers@acme.co.uk.", "careers@acme.co.uk")

    def test_case_is_normalised_and_duplicates_dropped(self):
        self.assertFinds("Jobs@Acme.COM or jobs@acme.com", "jobs@acme.com")

    def test_surrounding_punctuation(self):
        self.assertFinds("(jobs@acme.com), <hr@acme.com>; 'cv@acme.com'", "jobs@acme.com", "hr@acme.com", "cv@acme.com")

    def test_mailto_with_query(self):
        self.assertFinds("mailto:jobs@acme.com?subject=Ref%20123", "jobs@acme.com")

    def test_written_out_forms(self):
        for text in (
            "jobs [at] acme [dot] com",
            "jobs(at)acme.com",
            "jobs (AT) acme (DOT) com",
            "jobs {at} acme {dot} com",
            "jobs at acme dot com",
            "JOBS AT ACME DOT COM",
        ):
            with self.subTest(text):
                self.assertFinds(text, "jobs@acme.com")

    def test_multi_part_domains_when_written_out(self):
        self.assertFinds("hr [at] acme [dot] co [dot] uk", "hr@acme.co.uk")
        self.assertFinds("hr at acme dot co dot uk", "hr@acme.co.uk")

    def test_ordinary_sentences_are_not_addresses(self):
        for text in (
            "Apply at acme dot com today",
            "Look at acme.com for more",
            "Meet us at 5pm at the office",
            "Follow @acme.com on social",
            "https://medium.com/@john.doe/post",
        ):
            with self.subTest(text):
                self.assertFinds(text)

    def test_spaced_at_signs_never_invent_an_address(self):
        for text in (
            "Head of Talent @ Cal.com",
            "Senior Engineer @ Acme.io is hiring.",
            "Apply @ acme.co.uk/careers today",
            "Find us @ www.acme.co.uk or follow us",
            "machine learning @ scale.ai",
        ):
            with self.subTest(text):
                self.assertFinds(text)

    def test_written_out_forms_dont_guess(self):
        for text in (
            "Apply now at bluefin dot io",
            "To apply email sarah dot jones at bluefin dot com",
            "email jobs at bluefinco dot co.uk today",
        ):
            with self.subTest(text):
                self.assertFinds(text)

    def test_dotted_names_kept_whole(self):
        self.assertFinds("sarah dot jones [at] bluefin [dot] com", "sarah.jones@bluefin.com")
        self.assertFinds("sarah.jones at bluefin dot com", "sarah.jones@bluefin.com")
        self.assertFinds("vacancies at acme dot org dot uk", "vacancies@acme.org.uk")

    def test_capitals_inside_an_address_are_not_glued_text(self):
        self.assertFinds("sean.mcCarthy@acme.co.uk", "sean.mccarthy@acme.co.uk")
        self.assertFinds("jobs@talent.myCompany.com", "jobs@talent.mycompany.com")
        self.assertFinds("jobs@mail.brightPath.co.uk", "jobs@mail.brightpath.co.uk")
        self.assertFinds("Jobs@Acme.Com", "jobs@acme.com")
        self.assertFinds("careers@acme.co.UK", "careers@acme.co.uk")

    def test_next_sentence_glued_on(self):
        self.assertFinds("Send to careers@acme.com.Applications close Friday", "careers@acme.com")
        self.assertFinds("Email careers@acme.co.uk.We reply fast", "careers@acme.co.uk")
        self.assertFinds("Send your CV to careers@acme.co.uk-quote ref 77", "careers@acme.co.uk")

    def test_real_two_part_domains_kept(self):
        self.assertFinds("jobs@acme.com.au", "jobs@acme.com.au")
        self.assertFinds("a@b.co-op.uk", "a@b.co-op.uk")

    def test_long_blank_runs_stay_fast(self):
        self.assertFinds("x" + " " * 200_000 + "jobs@acme.com" + "\n" * 200_000, "jobs@acme.com")

    def test_hostile_repetition_stays_fast(self):
        for chunk in ("a.", "a-", "a+", "a'", "a at b dot "):
            with self.subTest(chunk):
                started = time.monotonic()
                emails.all_addresses(chunk * 100_000)
                self.assertLess(time.monotonic() - started, 5)

    def test_invisible_characters_removed(self):
        self.assertFinds("jobs​@acme‍.com", "jobs@acme.com")
        self.assertFinds("jobs＠acme.com", "jobs@acme.com")

    def test_word_glued_to_the_end(self):
        self.assertFinds("jobs@acme.comApply now", "jobs@acme.com")

    def test_no_reply_style_addresses_excluded(self):
        for address in (
            "noreply@acme.com", "no-reply@acme.com", "do-not-reply@acme.com", "donotreply@acme.com",
            "mailer-daemon@acme.com", "notifications@acme.com", "bounce@acme.com",
        ):
            with self.subTest(address):
                self.assertFinds(address)

    def test_mailboxes_that_dont_take_cvs_excluded(self):
        for address in ("privacy@acme.com", "dpo@acme.com", "support@acme.com", "data.protection@acme.com", "sales@acme.com"):
            with self.subTest(address):
                self.assertFinds(address)

    def test_junk_matches_excluded(self):
        for address in (
            "logo@2x.png", "sprite@3x.webp", "name@example.com", "you@yourcompany.com",
            "abc123@o123.ingest.sentry.io", "0123456789abcdef0123@acme.com",
        ):
            with self.subTest(address):
                self.assertFinds(address)

    def test_own_address_excluded(self):
        self.assertEqual(emails.find_emails("me@gmail.com jobs@acme.com", exclude="ME@gmail.com"), ["jobs@acme.com"])

    def test_empty_input(self):
        self.assertFinds("")
        self.assertFinds(None)


class ChooseRecipientTests(SimpleTestCase):
    def test_llm_choice_used_when_printed_in_the_text(self):
        self.assertEqual(emails.choose_recipient(["a@acme.com", "b@acme.com"], "B@Acme.com ", "", strict=True), "b@acme.com")

    def test_llm_cannot_add_an_address(self):
        self.assertEqual(emails.choose_recipient(["a@acme.com"], "attacker@evil.com", "", strict=True), "")
        self.assertEqual(emails.choose_recipient(["a@acme.com"], "attacker@evil.com", "", strict=False), "a@acme.com")

    def test_strict_needs_llm_confirmation(self):
        self.assertEqual(emails.choose_recipient(["info@acme.com"], "", "", strict=True), "")

    def test_no_candidates(self):
        self.assertEqual(emails.choose_recipient([], "", "", strict=False), "")
        self.assertEqual(emails.choose_recipient([], None, "", strict=False), "")

    def test_best_guess_prefers_recruiting_mailbox(self):
        self.assertEqual(emails.best_guess(["info@acme.com", "careers@acme.com"], ""), "careers@acme.com")

    def test_best_guess_uses_context(self):
        text = "Our office is ben@acme.com. To apply send your CV to sue@acme.com"
        self.assertEqual(emails.best_guess(["ben@acme.com", "sue@acme.com"], text), "sue@acme.com")
