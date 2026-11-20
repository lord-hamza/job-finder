"""Shared test setup. No test may reach the network, real Gmail or the real .env file."""
import io
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, TransactionTestCase, override_settings

from django.urls import reverse

from core import mailer, prompts
from core.models import Profile, Resume

CV_TEXT = (
    "Sam Taylor\n+44 7700 900123\nlinkedin.com/in/samtaylor\n"
    "Senior Python developer with 8 years of experience building Django APIs. "
    "Led a migration to AWS that cut hosting costs by 30 percent at Northwind Ltd. "
) * 2

JD_TEXT = (
    "Senior Python Developer (Remote, UK). Acme Ltd is hiring a developer to build Django APIs "
    "on AWS. You'll work with our platform team. To apply, send your CV to careers@acme.co.uk."
)

GOOD_BODY = " ".join(["Your platform team needs Django and AWS experience, and I have eight years of it."] * 5)


def gemini_job(**overrides):
    """A valid tailoring answer from Gemini."""
    job = {
        "is_job_posting": True,
        "title": "Senior Python Developer",
        "company": "Acme Ltd",
        "location": "Remote, UK",
        "work_setup": "remote",
        "based_in": "",
        "limited_to": [],
        "open_to_anywhere": False,
        "cv_scores": [],
        "rules_met": "yes",
        "match_score": 82,
        "reason": "Strong Django and AWS match. No Kubernetes in the CV.",
        "application_email": "careers@acme.co.uk",
        "subject": "Application for Senior Python Developer - Sam Taylor",
        "body": f"Dear Hiring Team,\n\n{GOOD_BODY}",
    }
    job.update(overrides)
    return job


def minimal_pdf(text, links=()):
    """A small real PDF containing the given text, with clickable links to the given addresses."""
    stream = f"BT /F1 12 Tf 20 700 Td ({text}) Tj ET".encode()
    annots = b" /Annots [%s]" % b" ".join(b"%d 0 R" % (6 + i) for i in range(len(links))) if links else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >>%s >>" % annots,
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    objects += [
        b"<< /Type /Annot /Subtype /Link /Rect [20 20 120 40] /A << /S /URI /URI (%s) >> >>" % link.encode()
        for link in links
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objects) + 1, xref))
    return out.getvalue()


class AppTestMixin:
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.env_file = Path(temp.name) / ".env"
        env = {
            "GEMINI_API_KEY": "test-gemini-key",
            "GEMINI_MODEL": "gemini-flash-latest",
            "GMAIL_ADDRESS": "sam@gmail.com",
            "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop",
            "MIN_MATCH_SCORE": "60",
            "DAILY_SEND_LIMIT": "25",
            "CRAWL_MAX_JOBS": "15",
        }
        for slot in range(2, mailer.MAX_ACCOUNTS + 1):  # your own .env may have more accounts: none of them here
            env.update(dict.fromkeys(mailer.env_keys(slot), ""))
        self._start(mock.patch.dict(os.environ, env))
        self._start(override_settings(ENV_FILE=self.env_file))
        self._start(mock.patch("core.pipeline.spawn", side_effect=lambda fn, *args: fn(*args)))
        self._start(mock.patch("core.pipeline.ensure_form_worker"))  # no test opens Chrome by accident
        from core import pipeline
        for remembered in (pipeline._tidying, pipeline._untidy):  # ids are reused from test to test
            remembered.clear()
        self._start(mock.patch("core.llm.time.sleep"))
        self._start(mock.patch("core.crawler.time.sleep"))
        self._start(
            mock.patch("requests.sessions.Session.request", side_effect=AssertionError("unexpected network call"))
        )

    def _start(self, patcher):
        patcher.enable() if hasattr(patcher, "enable") else patcher.start()
        self.addCleanup(patcher.disable if hasattr(patcher, "disable") else patcher.stop)

    CV_FIELDS = ("full_name", "email", "phone", "linkedin", "links", "country", "send_from", "facts")

    def make_profile(self, **overrides):
        """You, with one CV uploaded. resume_* overrides and the CV's own details go to the CV."""
        fields = {}
        cv = {
            "name": "my cv final (2).pdf",
            "data": b"%PDF-1.4 fake cv bytes",
            "text": CV_TEXT,
            "full_name": "Sam Taylor",
            "phone": "+44 7700 900123",
            "linkedin": "linkedin.com/in/samtaylor",
        }
        renamed = {"resume_name": "name", "resume_bytes": "data", "resume_text": "text"}
        for key, value in overrides.items():
            if key in renamed or key in self.CV_FIELDS:
                cv[renamed.get(key, key)] = value
            else:
                fields[key] = value
        profile = Profile.load()
        for key, value in fields.items():
            setattr(profile, key, value)
        profile.save()
        self.cv = Resume.objects.create(**cv)
        return profile


    PROFILE_FIELDS = ("onsite_places", "remote_countries", "cv_per_country", "allow_consent")

    def setup_form(self, cv=None, **changes):
        """What Setup posts, as the page shows it now, with changes: your settings by name, the rest for cv (self.cv
        if not given). True and False tick and untick a box."""
        profile = Profile.load()
        data = {"onsite_places": profile.onsite_places, "remote_countries": profile.remote_countries}
        data.update({box: "on" for box in ("cv_per_country", "allow_consent") if getattr(profile, box)})
        for each in Resume.objects.all():
            for field in ("full_name", "email", "phone", "linkedin", "links", "country"):
                data[f"cv-{each.pk}-{field}"] = getattr(each, field)
            data[f"cv-{each.pk}-facts"] = each.facts or prompts.FACTS_TEMPLATE  # as the page shows an empty one
            if mailer.accounts():  # the account box is only there while one is connected
                data[f"cv-{each.pk}-send_from"] = each.send_from
        target = cv or getattr(self, "cv", None)
        for key, value in changes.items():
            name = key if key in self.PROFILE_FIELDS else f"cv-{target.pk}-{key}"
            if value is False:
                data.pop(name, None)
            else:
                data[name] = "on" if value is True else value
        return data

    def save_setup(self, cv=None, **changes):
        return self.client.post(reverse("save_setup"), self.setup_form(cv, **changes), follow=True)


class AppTestCase(AppTestMixin, TestCase):
    pass


class RealBrowserTestCase(AppTestMixin, TransactionTestCase):
    """Django opens a second connection inside Playwright's loop, which a test transaction would lock out."""
