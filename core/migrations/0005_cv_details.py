import re

from django.db import migrations, models


def give_each_cv_its_details(apps, schema_editor):
    """Each CV gets the phone number and LinkedIn found in it. The facts you'd saved go to your first CV and any
    others start without, so what you wrote for one kind of job never goes out with another. A phone or LinkedIn
    you typed that no CV shows went out with all of them, so CVs without their own keep it. Drafts not yet sent
    are signed with their own CV's details."""
    Profile = apps.get_model("core", "Profile")
    Resume = apps.get_model("core", "Resume")
    Application = apps.get_model("core", "Application")
    cvs = list(Resume.objects.order_by("created_at", "id"))
    if not cvs:
        return
    from core.resume import contact_details, guess_contact  # only needed when there are CVs to read

    for cv in cvs:
        try:
            found = contact_details(cv.name, bytes(cv.data), cv.text)
        except Exception:
            found = {"phone": "", "linkedin": ""}
        cv.phone, cv.linkedin = found["phone"][:40], found["linkedin"][:200]
    profile = Profile.objects.order_by("pk").first()
    if profile:
        cvs[0].facts = profile.facts
        for key in ("phone", "linkedin"):
            saved = getattr(profile, key)
            if saved and len(cvs) == 1:  # with one CV, a detail you typed can only be about that CV
                filled_in = saved == guess_contact(cvs[0].text)[key] or _same(key, saved, getattr(cvs[0], key), cut_off=True)
                if not filled_in:  # what the app filled in from the CV itself is read again, more carefully
                    setattr(cvs[0], key, saved)
            elif saved and not any(_same(key, saved, getattr(cv, key)) for cv in cvs):
                for cv in cvs:
                    setattr(cv, key, getattr(cv, key) or saved)
    for cv in cvs:
        cv.save(update_fields=["phone", "linkedin", "facts"])
        if profile:
            _resign(Application, cv, _contact(profile.phone, profile.linkedin), _contact(cv.phone, cv.linkedin))


def _same(key, saved, value, cut_off=False):
    """The same number or LinkedIn, written differently. cut_off: also an address that stops part of the way."""
    if not value:
        return False
    if key == "phone":
        return re.sub(r"\D", "", saved)[-9:] == re.sub(r"\D", "", value)[-9:]
    slug = lambda url: re.sub(r"^.*?linkedin\.com/in/", "", url, flags=re.I).strip("/").lower()  # noqa: E731
    return slug(saved) == slug(value) or (cut_off and bool(slug(saved)) and slug(value).startswith(slug(saved)))


def _contact(phone, linkedin):
    return " | ".join(part for part in (phone, linkedin) if part)


def _resign(Application, cv, old, new):
    """Drafts not yet sent were signed with the details all CVs shared, as the app wrote them, whatever the name."""
    if old == new:
        return
    ending = re.compile(r"\n\nKind regards,\n([^\n]+)" + (re.escape("\n" + old) if old else "") + r"\Z")
    drafts = Application.objects.filter(resume=cv).exclude(status__in=["sent", "sending", "filling"]).exclude(note__in=_MAYBE_SENT)
    for app in drafts:
        match = ending.search(app.body)
        if match:  # unless you'd changed how it ends
            app.body = f"{app.body[: match.start()]}\n\nKind regards,\n{match.group(1)}" + (f"\n{new}" if new else "")
            app.save(update_fields=["body"])


# The notes the app gave rows that may already have gone out, as they were written when this migration was.
_MAYBE_SENT = [
    "Interrupted while sending. Check your Gmail Sent folder before sending again.",
    "Interrupted while filling in the form. It may have been submitted, so check your email before trying again.",
    "The site showed a CAPTCHA after Submit. Solve it in the Chrome window, then press I submitted it.",
    "The site emailed you a code. Enter it in the Chrome window, then press I submitted it.",
    "Pressed Submit, but the site didn't confirm it. Check the Chrome window before trying again, so you don't apply twice.",
]


def back_to_shared_details(apps, schema_editor):
    """Undoing this: your first CV's phone and LinkedIn become the shared ones again, with the first facts saved."""
    Profile = apps.get_model("core", "Profile")
    Resume = apps.get_model("core", "Resume")
    profile = Profile.objects.order_by("pk").first()
    cvs = list(Resume.objects.order_by("created_at", "id"))
    if profile and cvs:
        profile.phone, profile.linkedin = cvs[0].phone, cvs[0].linkedin
        profile.facts = next((cv.facts for cv in cvs if cv.facts), "")
        profile.save(update_fields=["phone", "linkedin", "facts"])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_many_cvs'),
    ]

    operations = [
        migrations.AddField(
            model_name='resume',
            name='phone',
            field=models.CharField(blank=True, max_length=40),
        ),
        migrations.AddField(
            model_name='resume',
            name='linkedin',
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name='resume',
            name='facts',
            field=models.TextField(blank=True),
        ),
        migrations.RunPython(give_each_cv_its_details, back_to_shared_details),
        migrations.RemoveField(model_name='profile', name='phone'),
        migrations.RemoveField(model_name='profile', name='linkedin'),
        migrations.RemoveField(model_name='profile', name='facts'),
    ]
