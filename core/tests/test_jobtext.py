"""Laying out a job advert so it's easy to read, without changing what it says."""
from django.test import SimpleTestCase

from core import jobtext


class BlocksTests(SimpleTestCase):
    def kinds(self, text):
        return [(block["kind"], block.get("text") or block["items"]) for block in jobtext.blocks(text)]

    def test_linkedin_style_post(self):
        text = (
            "\U0001d416\U0001d41e \U0001d41a\U0001d42b\U0001d41e \U0001d421\U0001d422\U0001d42b\U0001d422\U0001d427\U0001d420!\n"
            "Acme is looking for an engineer.\n\n\U0001d416\U0001d421\U0001d41a\U0001d42d \U0001d418\U0001d428\U0001d42e'\U0001d425\U0001d425 \U0001d403\U0001d428:\n"
            " \u2705 Build APIs\n \u2705\ufe0f Ship agents\n\U0001f4cd Location: Lahore"
        )
        self.assertEqual(self.kinds(text), [
            ("heading", "We are hiring!"), ("paragraph", "Acme is looking for an engineer."), ("heading", "What You'll Do"),
            ("list", ["Build APIs", "Ship agents", "Location: Lahore"]),
        ])

    def test_marks_numbers_and_headings(self):
        text = "RESPONSIBILITIES\n- Own the pipeline\n* Write tests\n1. Ship\n2) Measure\n## Benefits\n\u2022 Health\n**Perks**\n\u2013 Laptop"
        self.assertEqual(self.kinds(text), [
            ("heading", "RESPONSIBILITIES"), ("list", ["Own the pipeline", "Write tests", "Ship", "Measure"]),
            ("heading", "Benefits"), ("list", ["Health"]), ("heading", "Perks"), ("list", ["Laptop"]),
        ])

    def test_text_that_only_looks_like_a_mark_stays_as_it_is(self):
        text = "$120k to $150k a year\n+ a yearly bonus\n#1 in our market\n-5 degrees in winter\nWe hire in Lahore: apply now and join."
        self.assertEqual([kind for kind, _ in self.kinds(text)], ["paragraph"] * 5)

    def test_a_sentence_broken_across_lines_is_joined(self):
        text = "We build tools that help\nsupport teams answer faster.\nNext sentence here.\n\nnew paragraph after a gap."
        self.assertEqual(self.kinds(text), [
            ("paragraph", "We build tools that help support teams answer faster."), ("paragraph", "Next sentence here."),
            ("paragraph", "new paragraph after a gap."),
        ])

    def test_empty(self):
        self.assertEqual(jobtext.blocks(""), [])
        self.assertEqual(jobtext.blocks(None), [])


class FaithfulTests(SimpleTestCase):
    ORIGINAL = (
        "Home Jobs Login\nAI Engineer\nWe are looking for an AI engineer to build retrieval pipelines and agents.\n"
        "You will work with product and design to ship features every week.\n\u2705 Python\n\u2705 FastAPI and Docker\nPrivacy Terms"
    )

    def test_dropping_clutter_and_laying_out_is_fine(self):
        tidied = jobtext.markup([
            {"kind": "heading", "text": "AI Engineer"},
            {"kind": "paragraph", "text": "We are looking for an AI engineer to build retrieval pipelines and agents."},
            {"kind": "paragraph", "text": "You will work with product and design to ship features every week."},
            {"kind": "bullet", "text": "Python"}, {"kind": "bullet", "text": "FastAPI and Docker"},
        ])
        self.assertTrue(jobtext.faithful(tidied, self.ORIGINAL))
        self.assertEqual(jobtext.blocks(tidied)[-1], {"kind": "list", "items": ["Python", "FastAPI and Docker"]})

    def test_rewording_or_adding_is_not(self):
        reworded = "Join our dynamic team to craft cutting edge AI retrieval systems and intelligent agents every single week."
        self.assertFalse(jobtext.faithful(reworded, self.ORIGINAL))

    def test_dropping_most_of_the_advert_is_not(self):
        long_original = self.ORIGINAL + "\n" + "We offer a pension, health cover and a learning budget for every engineer. " * 10
        self.assertFalse(jobtext.faithful("## AI Engineer\n\nWe are looking for an AI engineer to build retrieval pipelines and agents.", long_original))

    def test_odd_answers_make_nothing(self):
        self.assertEqual(jobtext.markup(None), "")
        self.assertEqual(jobtext.markup(["x", {"kind": "bullet"}, {"text": "  "}]), "")


class PageTests(SimpleTestCase):
    def test_a_whole_page_is_noisy_and_an_advert_isnt(self):
        page = "\n".join(["Home", "Jobs", "Sign in", "Pricing"] * 10) + "\nWe are hiring an engineer to build our platform.\n"
        self.assertTrue(jobtext.noisy(page))
        self.assertFalse(jobtext.noisy("We are hiring.\nWhat you'll do:\n- Build things\n- Ship things\n" * 3))

    def test_facts_from_the_jobs_published_data(self):
        posting = {"hiringOrganization": {"name": "Acme"}, "jobLocation": [{"address": {"addressLocality": "Austin", "addressRegion": "TX"}}],
                   "employmentType": ["FULL_TIME", "CONTRACTOR"], "jobLocationType": "TELECOMMUTE", "validThrough": "2026-12-01T00:00"}
        self.assertEqual(jobtext.facts(posting), ["Acme", "Austin, TX", "FULL TIME, CONTRACTOR", "Remote", "Closes 2026-12-01"])

    def test_page_parts(self):
        text = 'Page title: T\n\nStructured job data:\n[{"title": "X", "description": "D"}]\n\nVisible\n\nEmail links on this page:\na@b.c'
        self.assertEqual(jobtext.page_parts(text), ("T", [{"title": "X", "description": "D"}], "Visible"))
        self.assertEqual(jobtext.page_parts("Structured job data:\n[broken")[1], [])


class DeclutterTests(SimpleTestCase):
    def test_menus_and_buttons_go_and_the_advert_stays(self):
        page = "Home\nJobs\nLog in\nAI Engineer\nWe build agents for support teams.\nResponsibilities\nShip it\nHome\nJobs\nPrivacy"
        self.assertEqual(jobtext.declutter(page), "AI Engineer\nWe build agents for support teams.\nResponsibilities\nShip it")

    def test_a_tidied_copy_of_a_mostly_menu_page_is_still_faithful(self):
        page = "\n".join(["Home", "Jobs", "About", "Log in"] * 20) + "\nWe build agents for support teams and need an engineer to own retrieval end to end."
        tidied = "## AI Engineer\n\nWe build agents for support teams and need an engineer to own retrieval end to end."
        self.assertTrue(jobtext.faithful(tidied, page))
