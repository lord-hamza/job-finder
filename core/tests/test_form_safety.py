"""Hostile pages that try to trick the form filler, in real headless Chrome. Gemini is faked as careless on purpose."""
from unittest import SkipTest, mock

from django.test import SimpleTestCase

from core import formfill
from core.formfill import Answer
from core.tests.fake_site import CONTACT, CV, PAGES, SUBMIT, FakeSite
from core.tests.test_formfill import chrome_available

ACTION = 'action="/submit" method="post" enctype="multipart/form-data"'


def form(inner="", button=SUBMIT, attrs=ACTION):
    return f"<title>Apply</title><form {attrs}>{CONTACT}{CV}{inner}{button}</form>"


def alert_after_click(message):
    script = f"event.preventDefault(); const a = document.createElement('div'); a.setAttribute('role', 'status'); a.textContent = {message!r}; document.body.append(a);"
    return form(attrs=f'onsubmit="{script}" enctype="multipart/form-data"')


def careless(fields):
    """Gemini gone wrong: it answers everything and claims the CV says so."""
    replies = {}
    for item in fields:
        if item.options:
            replies[item.key] = Answer([item.options[0]], "cv")
        else:
            replies[item.key] = Answer(["CARELESS"], "cv")
    return replies


# Pressing the wrong button
WRONG_BUTTONS = {
    "/withdraw": '<button formaction="/withdraw">Withdraw my application</button>',
    "/cancel": '<button formaction="/cancel">Cancel</button>',
    "/draft": '<button formaction="/draft">Save Draft</button>',
    "/delete": '<input type="submit" formaction="/delete" value="Delete my profile">',
    "/back": '<button type="button">Next</button><button formaction="/back">Back</button>',
    "/account": '<button formaction="/account">Create account</button>',
    "/done": '<button formaction="/done">Done</button>',
}
# Must press Submit, not the one after it
DECOYS = {
    "/complaint-last": '<button formaction="/complaint">Submit a complaint</button>',
    "/friend-last": '<button formaction="/friend">Send to a friend</button>',
    "/later-last": '<button formaction="/later">Complete later</button>',
    "/draft-last": '<button formaction="/draft">Save Draft</button>',
}
# Text that looks like success but isn't
FALSE_SUCCESS = {
    "/not-possible": "Sorry, your application was received was not possible",
    "/date": "Application received date is required",
    "/not-confirmed": "We could not confirm that your application was submitted. Please try again.",
    "/review": "Review your answers. Once your application has been submitted it cannot be changed.",
    "/sent-back": "Your application was sent back: 3 fields are invalid.",
}
HOLD_BACK = {
    # Required, in ways that aren't the required attribute
    "/star-css": (form('<style>.star::after{content:"*"}</style><label for="q" class="star">Team</label><input id="q" name="q">'), "Team"),
    "/star-data": (form('<label for="q">Team</label><input id="q" name="q" data-required="true">'), "Team"),
    "/star-help": (form('<div><label for="q">Team</label><input id="q" name="q"><small>Required</small></div>'), "Team"),
    "/star-class": (form('<div class="form-group required"><label for="q">Team</label><input id="q" name="q"></div>'), "Team"),
    "/star-aria-group": (form('<p>Team</p><div role="radiogroup" aria-required="true"><label><input type="radio" name="t" value="a">A</label></div>'), "Team"),
    # Widgets it can't fill
    "/custom-radio": (form('<p>Team*</p><div role="radiogroup" aria-required="true"><div role="radio" aria-checked="false" style="height:20px">A</div></div>'), "required question"),
    "/custom-listbox": (form('<label id="y">Years*</label><button type="button" aria-haspopup="listbox" aria-labelledby="y" style="height:20px">Pick</button>'), "required question"),
    "/editable": (form('<label id="w">Why this job?*</label><div contenteditable="true" role="textbox" aria-labelledby="w" style="height:40px"></div>'), "required question"),
    "/shadow": (form('<x-q id="s" style="display:block;height:40px"></x-q><script>document.getElementById("s").attachShadow({mode:"open"}).innerHTML="<input required>";</script>'), "can't be read"),
    # Other CAPTCHAs
    "/captcha-widget": (form('<div class="g-recaptcha" style="width:304px;height:78px"></div>'), "CAPTCHA"),
    "/captcha-iframe": (form('<iframe src="/fake-captcha?arkoselabs" width="300" height="100"></iframe>'), "CAPTCHA"),
    "/captcha-typed": (form('<label for="c">Type the characters you see*</label><input id="c" name="check" required>'), "CAPTCHA"),
    "/captcha-sum": (form('<label for="c">Security question: what is 3 + 4?*</label><input id="c" name="human" required>'), "CAPTCHA"),
    # Agreements, however they're asked
    "/consent-select": (form('<label for="p">Do you consent to us processing your data?*</label><select id="p" name="p" required><option value="">Select</option><option>Yes, I consent</option><option>No</option></select>'), "agree"),
    "/consent-radio": (form('<fieldset><legend>Do you agree to the privacy policy?*</legend><label><input type="radio" name="p" value="y" required>I agree</label><label><input type="radio" name="p" value="n">I do not agree</label></fieldset>'), "agree"),
    "/consent-group": (form('<fieldset><legend>Please confirm*</legend><label><input type="checkbox" name="a" required>I agree to the terms</label><label><input type="checkbox" name="b" required>I consent to data processing</label></fieldset>'), "agree"),
    "/consent-keep": (form('<label><input type="checkbox" name="pool" required>You may keep my CV for 5 years and share it with partners*</label>'), "agree"),
    "/consent-tandc": (form('<label><input type="checkbox" name="tc" required>I accept the T&amp;Cs*</label>'), "agree"),
    "/consent-hidden-chars": (form('<label><input type="checkbox" name="tc" required>I con​sent to the te​rms*</label>'), "agree"),
    # Sensitive details, however they're labelled
    "/sensitive-placeholder": (form('<label for="s">Reference*</label><input id="s" name="x" required placeholder="Passport number">'), "sensitive"),
    "/sensitive-name": (form('<label for="s">Number*</label><input id="s" name="iban_number" required>'), "sensitive"),
    "/sensitive-camel": (form('<label for="s">Value*</label><input id="s" name="dateOfBirth" required>'), "sensitive"),
    "/sensitive-split": (form('<label for="s"><span>Da</span><span>te of bi</span><span>rth</span>*</label><input id="s" name="b" required>'), "sensitive"),
    "/sensitive-autocomplete": (form('<label for="s">Long number*</label><input id="s" name="a" required autocomplete="cc-number">'), "sensitive"),
    # Personal questions in other words
    "/personal-sex": (form('<label for="m">Are you male or female?*</label><select id="m" name="m" required><option value="">Select</option><option>Male</option><option>Female</option></select>'), "personal answer"),
    "/personal-disabled": (form('<label for="d">Do you consider yourself disabled?*</label><select id="d" name="d" required><option value="">Select</option><option>Yes</option><option>No</option></select>'), "personal answer"),
    # Asked for only the user can answer, so the careless "the CV says so" must be refused
    "/facts-permitted": (form('<label for="f">Are you permitted to work in the UK?*</label><select id="f" name="f" required><option value="">Select</option><option>Yes</option><option>No</option></select>'), "permitted to work"),
    "/facts-uk-based": (form('<label for="f">Are you UK-based?*</label><input id="f" name="f" required>'), "UK-based"),
    "/facts-name-only": (form('<label for="f">Please answer*</label><input id="f" name="rightToWork" required>'), "Please answer"),
    "/facts-ote": (form('<label for="f">What is your current OTE?*</label><input id="f" name="f" required>'), "OTE"),
    "/facts-offices": (form('<label for="f">Are you able to commit to working from one of our offices each week?*</label><input id="f" name="f" required>'), "offices"),
    # Forms that aren't ready to send
    "/no-cv-box": (f'<title>Apply</title><form {ACTION}>{CONTACT}<label for="q">Team</label><input id="q" name="q">{SUBMIT}</form>', "CV isn't attached"),
    "/late-question": (form('<label for="c">Team</label><select id="c" name="c" onchange="setTimeout(() => document.getElementById(\'w\').style.display = \'block\', 700)"><option value="">Select</option><option>A</option></select><div id="w" style="display:none"><label for="l">Late question*</label><input id="l" name="l" required></div>'), "Late question"),
    "/bad-email": (f'<title>Apply</title><form {ACTION}>{CONTACT}{CV}<label for="u">Code*</label><input id="u" name="u" required pattern="[0-9]+">{SUBMIT}</form>', "Code"),
}
# Pages the filler must leave completely alone, even when only filling in
NEVER_CLICKED = {
    "/ok-submits": '<h1>Quick apply</h1><form action="/submit" method="post"><label>Your email<input name="email" type="email"></label><button>OK</button></form>',
    "/apply-small-form": '<h1>Quick apply</h1><form action="/submit" method="post"><label>Your name<input name="name"></label><button>Apply</button></form>',
    "/decline-offer": form() .replace(SUBMIT, "") + '<p>You have a job offer from Acme.</p><button onclick="fetch(\'/submit-json\',{method:\'POST\',body:\'declined\'})">Decline</button>',
    "/accept-terms": '<div id="modal"><h2>Candidate terms</h2><p>We may share your data with our partners for 10 years.</p><button onclick="fetch(\'/submit-json\',{method:\'POST\',body:\'accepted\'})">I accept</button></div>',
}


class SafetyTestCase(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not chrome_available():
            raise SkipTest("Google Chrome isn't installed")
        PAGES.update({path: form(button=button) for path, button in WRONG_BUTTONS.items()})
        PAGES.update({path: form(button=SUBMIT + button) for path, button in DECOYS.items()})
        PAGES.update({path: alert_after_click(message) for path, message in FALSE_SUCCESS.items()})
        PAGES.update({path: page for path, (page, _) in HOLD_BACK.items()})
        PAGES.update(NEVER_CLICKED)
        PAGES.update({
            "/yes-no-submits": form('<p class="question">Have you used Django?*</p><div><button>Yes</button><button>No</button></div>'),
            "/combobox-submits": form('<label id="l">Team*</label><button role="combobox" aria-labelledby="l" formaction="/submit">Pick a team</button>'),
            "/honeypots": form(
                '<div style="position:absolute;left:-9999px"><label for="h1">Email</label><input id="h1" name="h_off"></div>'
                '<div style="opacity:0;height:1px;overflow:hidden"><label for="h2">Phone</label><input id="h2" name="h_opacity"></div>'
                '<div aria-hidden="true" style="height:1px;overflow:hidden"><label for="h3">Email address</label><input id="h3" name="h_aria"></div>'
                '<label for="h4">Your name</label><input id="h4" name="h_tab" style="height:1px;width:1px;border:0;padding:0" tabindex="-1">'
                '<label for="h5">Leave this blank</label><input id="h5" name="h_blank">'
            ),
            "/other-peoples-details": form(
                '<label for="o1">Supervisor email</label><input id="o1" name="o1"><label for="o2">Partner email</label><input id="o2" type="email" name="o2">'
                '<fieldset><legend>Referee 1</legend><label for="o3">Name</label><input id="o3" name="o3"><label for="o4">Email</label><input id="o4" name="o4" type="email"></fieldset>'
                '<h3>In an emergency, who should we call?</h3><div><label for="o5">Name</label><input id="o5" name="o5"><label for="o6">Phone</label><input id="o6" name="o6" type="tel"></div>'
            ),
            "/short-box": form('<label for="o">Mobile</label><input id="o" name="mobile2" maxlength="4">'),
            "/precheck": form('<label><input type="checkbox" name="marketing" checked>I agree to receive marketing emails</label>'),
            "/cv-elsewhere": form('<label for="a">Upload your CV to share with our partners</label><input id="a" name="share" type="file"><label for="b">Referee CV</label><input id="b" name="referee" type="file">'),
            "/cover-trick": form('<label for="m">Do you require sponsorship?*</label><textarea id="m" name="cover_letter" required></textarea>'),
            "/additional-trick": form('<label for="m">Additional information about any unspent convictions*</label><textarea id="m" name="m" required></textarea>'),
            "/referral-form": '<h1>Refer a friend</h1>' + form('<label for="n">Friend\'s name</label><input id="n" name="f">'),
            "/complaint-form": '<h1>Complaints</h1>' + form('<label for="n">What went wrong?</label><textarea id="n" name="f"></textarea>'),
            "/late-success": form(attrs='onsubmit="event.preventDefault()" enctype="multipart/form-data"') + '<div id="b"></div><script>setTimeout(() => document.getElementById("b").innerText = "Thank you for applying!", 3500);</script>',
            "/bad-redirect": form(attrs='action="/submit" method="post" enctype="multipart/form-data"').replace('/submit', '/submit-json'),
            "/long-answer": form('<label for="c">Favourite colour</label><input id="c" name="c">'),
            "/place-trap": form('<label for="l">Current location*</label><input id="l" name="location" required autocomplete="off"><div id="d"></div><script>const l = document.getElementById("l"); l.addEventListener("input", () => { document.getElementById("d").innerHTML = \'<div role="option" style="height:20px">Karachi, Mississippi, USA</div><div role="option" style="height:20px" onclick="l.value=this.textContent">Karachi, Sindh, Pakistan</div>\'; });</script>'),
        })
        cls.site = FakeSite()
        cls.addClassCleanup(cls.site.close)
        patcher = mock.patch("core.formfill.is_public", return_value=True)
        patcher.start()
        cls.addClassCleanup(patcher.stop)
        for name, value in (("LATE_QUESTION_MS", 900), ("CV_READ_MS", 100)):
            delay = mock.patch(f"core.formfill.{name}", value)
            delay.start()
            cls.addClassCleanup(delay.stop)
        cls.chrome = formfill.Chrome(headless=True)
        cls.chrome.__enter__()
        cls.addClassCleanup(cls.chrome.__exit__, None, None, None)

    def setUp(self):
        self.site.submissions.clear()
        self.addCleanup(self._close_windows)

    def _close_windows(self):
        for context, _, temp in self.chrome._kept:
            formfill._close(context)
            temp.cleanup()
        self.chrome._kept = []

    def apply(self, path, ask=careless, submit=True, allow_consent=False):
        job = formfill.Job(
            url=self.site.url + path, full_name="Sam Taylor", email="sam@gmail.com", phone="+44 7700 900123",
            linkedin="linkedin.com/in/sam", message="Dear Hiring Team, please see my CV.", resume_name="Sam_CV.pdf",
            resume_bytes=b"%PDF cv", cover_name="Sam_Letter.docx", cover_bytes=b"PK", allow_consent=allow_consent,
            submit=submit, stay_open=False, ask=ask,
        )
        return formfill.apply(self.chrome, job)


class ButtonTests(SafetyTestCase):
    def test_only_a_button_that_means_submit_is_ever_pressed(self):
        for path in WRONG_BUTTONS:
            with self.subTest(path):
                self.site.submissions.clear()
                outcome = self.apply(path)
                self.assertEqual(outcome.state, formfill.NEEDS_YOU, outcome.note)
                self.assertEqual(self.site.submissions, [], outcome.note)

    def test_real_submit_is_chosen_over_buttons_after_it(self):
        for path in DECOYS:
            with self.subTest(path):
                self.site.submissions.clear()
                outcome = self.apply(path)
                self.assertEqual(outcome.state, formfill.SUBMITTED, outcome.note)
                self.assertEqual(len(self.site.submissions), 1)

    def test_buttons_that_would_send_the_form_early_are_never_pressed(self):
        for path in ("/yes-no-submits", "/combobox-submits"):
            with self.subTest(path):
                self.site.submissions.clear()
                outcome = self.apply(path)
                self.assertEqual(outcome.state, formfill.NEEDS_YOU)
                self.assertIn("could send the form early", outcome.note)
                self.assertEqual(self.site.submissions, [])

    def test_nothing_is_pressed_just_to_get_past_a_page(self):
        for path in NEVER_CLICKED:
            with self.subTest(path):
                self.site.submissions.clear()
                outcome = self.apply(path, submit=False)
                self.assertNotEqual(outcome.state, formfill.SUBMITTED)
                self.assertEqual(self.site.submissions, [], outcome.note)


class FalseSuccessTests(SafetyTestCase):
    def test_words_that_only_look_like_confirmation(self):
        for path in FALSE_SUCCESS:
            with self.subTest(path), mock.patch("core.formfill.RESULT_SECONDS", 2):
                outcome = self.apply(path)
                self.assertEqual(outcome.state, formfill.NEEDS_YOU, outcome.note)

    def test_success_text_without_anything_sent_doesnt_count(self):
        with mock.patch("core.formfill.RESULT_SECONDS", 5):
            outcome = self.apply("/late-success")
        self.assertEqual(outcome.state, formfill.NEEDS_YOU, outcome.note)

    def test_a_good_response_with_no_confirmation_doesnt_count(self):
        with mock.patch("core.formfill.RESULT_SECONDS", 2):
            outcome = self.apply("/bad-redirect")
        self.assertEqual(outcome.state, formfill.NEEDS_YOU, outcome.note)
        self.assertEqual(len(self.site.submissions), 1)


class HoldBackTests(SafetyTestCase):
    def test_every_trap_stops_before_submit(self):
        for path, (_, words) in HOLD_BACK.items():
            with self.subTest(path):
                self.site.submissions.clear()
                outcome = self.apply(path, ask=careless if path.startswith(("/facts", "/no-cv", "/late", "/bad")) else (lambda fields: {}))
                self.assertEqual(outcome.state, formfill.NEEDS_YOU, outcome.note)
                self.assertIn(words, outcome.note)
                self.assertEqual(self.site.submissions, [], "nothing may reach the employer")

    def test_permission_covers_agreements_asked_as_a_choice(self):
        for path, field_name, expected in (("/consent-select", "p", "Yes, I consent"), ("/consent-radio", "p", "y")):
            with self.subTest(path):
                self.site.submissions.clear()
                outcome = self.apply(path, allow_consent=True)
                self.assertEqual(outcome.state, formfill.SUBMITTED, outcome.note)
                self.assertEqual(self.site.submissions[0][field_name], expected)

    def test_permission_never_covers_eligibility(self):
        outcome = self.apply("/facts-permitted", allow_consent=True)
        self.assertEqual(outcome.state, formfill.NEEDS_YOU)
        self.assertEqual(self.site.submissions, [])


class WhatGoesInTests(SafetyTestCase):
    def test_trap_fields_stay_empty(self):
        outcome = self.apply("/honeypots", ask=lambda fields: {})
        self.assertEqual(outcome.state, formfill.SUBMITTED, outcome.note)
        sent = self.site.submissions[0]
        for name in ("h_off", "h_opacity", "h_aria", "h_tab", "h_blank"):
            with self.subTest(name):
                self.assertEqual(sent[name], "")

    def test_other_peoples_details_arent_given_the_applicants(self):
        outcome = self.apply("/other-peoples-details", ask=lambda fields: {})
        self.assertEqual(outcome.state, formfill.SUBMITTED, outcome.note)
        sent = self.site.submissions[0]
        for name in ("o1", "o2", "o3", "o4", "o5", "o6"):
            with self.subTest(name):
                self.assertEqual(sent[name], "")

    def test_values_are_never_cut_short(self):
        self.apply("/short-box", ask=lambda fields: {})
        self.assertEqual(self.site.submissions[0]["mobile2"], "")

    def test_pre_ticked_marketing_box_is_unticked(self):
        self.apply("/precheck", ask=lambda fields: {})
        self.assertNotIn("marketing", self.site.submissions[0])

    def test_cv_only_goes_in_the_cv_box(self):
        self.apply("/cv-elsewhere", ask=lambda fields: {})
        sent = self.site.submissions[0]
        self.assertEqual(sent["resume"], ("Sam_CV.pdf", b"%PDF cv"))
        self.assertEqual(sent["share"], "")
        self.assertEqual(sent["referee"], "")

    def test_cover_note_only_goes_in_a_box_labelled_for_it(self):
        for path in ("/cover-trick", "/additional-trick"):
            with self.subTest(path):
                self.site.submissions.clear()
                outcome = self.apply(path, ask=lambda fields: {})
                self.assertEqual(outcome.state, formfill.NEEDS_YOU, outcome.note)
                self.assertEqual(self.site.submissions, [])

    def test_absurdly_long_answer_rejected(self):
        self.apply("/long-answer", ask=lambda fields: {fields[0].key: Answer(["THE WHOLE CV " * 200], "cv")})
        self.assertEqual(self.site.submissions[0]["c"], "")

    def test_place_must_match_every_part(self):
        ask = lambda fields: {fields[0].key: Answer(["Karachi, Pakistan"], "facts")}  # noqa: E731
        outcome = self.apply("/place-trap", ask=ask)
        self.assertEqual(outcome.state, formfill.SUBMITTED, outcome.note)
        self.assertEqual(self.site.submissions[0]["location"], "Karachi, Sindh, Pakistan")

    def test_same_place_rules(self):
        cases = [("Karachi, Sindh, Pakistan", "Karachi, Pakistan", True), ("Karachi, Mississippi, USA", "Karachi, Pakistan", False),
                 ("Romania", "Oman", False), ("Oman", "Oman", True), ("Bathgate, Scotland", "Bath, UK", False),
                 ("Bath, England", "Bath, UK", True), ("London, United Kingdom", "London, UK", True),
                 ("Karachi, Sindh, PAK", "Karachi, Pakistan", True), ("Karachi, Orenburgskaya oblast', RUS", "Karachi, Pakistan", False),
                 ("London, GBR", "London, United Kingdom", True), ("Pakistan", "Pakistan", True),
                 ("United Kingdom", "United Kingdom", True), ("United Kingdom", "UK", True),
                 ("Belfast, United Kingdom", "United Kingdom", False), ("Kingston, GBR", "UK", False)]
        for option, value, expected in cases:
            with self.subTest(option=option, value=value):
                self.assertEqual(formfill._same_place(option, value), expected)


class NotAnApplicationTests(SafetyTestCase):
    def test_a_file_box_alone_doesnt_make_it_an_application(self):
        for path in ("/referral-form", "/complaint-form"):
            with self.subTest(path):
                self.site.submissions.clear()
                outcome = self.apply(path)
                self.assertEqual(outcome.state, formfill.NO_FORM, outcome.note)
                self.assertEqual(self.site.submissions, [])
