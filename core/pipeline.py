"""The workflow: read a job, tailor an application, decide what to do, send it."""
import hashlib
import logging
import os
import re
import threading
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connections
from django.utils import timezone

from . import crawler, emails, formfill, llm, mailer, prompts, resume
from .models import IN_FLIGHT, Application, CrawlRun, Profile, Resume

log = logging.getLogger(__name__)

Status = Application.Status
Source = Application.Source

CV_CHARS = 15_000
JD_CHARS = 25_000
TRIAGE_CHARS = 3_000  # picking links off a page only needs the gist
STORED_JD_CHARS = 100_000
MAX_FAILS_IN_A_ROW = 3
MIN_BODY_WORDS = 40
BOOT_TIME = timezone.now()
INTERRUPTED_SEND = "Interrupted while sending. Check your Gmail Sent folder before sending again."
UNCONFIRMED_ADDRESS = "Gemini couldn't confirm this address takes applications. Check it, then click Send."
INTERRUPTED_FORM = (
    "Interrupted while filling in the form. It may have been submitted, so check your email before trying again."
)

_send_lock = threading.Lock()  # a cap check and its send must not interleave with another's
_PLACEHOLDER = re.compile(
    r"\[[^\]\n\d]{1,60}\]|_{3,}|\bX{2,}\b|\{\{?[^}\n]{1,40}\}?\}|<[A-Za-z][^>\n]{0,40}>"
    r"|\b(your name|company name|job title|lorem ipsum|insert here)\b",
    re.I,
)
# Wording that means a job needs the office, whatever Gemini made of the posting.
_OFFICE_WORK = re.compile(
    r"\bhybrid (role|work|working|position|job|schedule|arrangement|policy|set-?up|pattern|basis|contract|model|way of working)\b"
    r"|[(\[|/,:\u2013\u2014-]\s*(hybrid|on-?site)\b(?!\s+(cloud|multi|and|or|environment|infrastructure|app|mobile|search))"
    r"|\b(hybrid|on-?site)\s*[)\]|,:\u2013\u2014-](?!\s*(cloud|multi))"
    r"|^\s*(hybrid|on-?site)\s*$"
    r"|\bhybrid with\b"
    r"|\b(one|two|three|four|once|twice|\d(\s*-\s*\d)?)\s*(x\s*)?(days?|times?)\s*(a|per|each|every)?\s*(week|month|fortnight)?\s*"
    r"(in|at|from|to)\s*(the|our|an?)?\s*(\w+\s)?(office|site|hq|headquarters|studio)\b"
    r"|\b(once|twice)\s+(a|per|each|every)\s+(week|month|fortnight)\s+(in|at|from|to)\s+(the|our|an?)?\s*(\w+\s)?(office|site|hq|headquarters|studio)\b"
    r"|\b(office|site)[- ]based\b|\bbased (in|at|from|out of) (the|our) (\w+ )?office\b|\bin[- ]office\b"
    r"|\bon-?site (role|position|job|work|working|presence|attendance)\b|\boffice attendance\b"
    r"|\battend(ing)? (the|our|an?) (\w+ )?office\b|\bcommut(able|ing) distance\b|\bwithin commuting\b",
    re.I | re.M,
)
RULES_NOT_MET = "Doesn't meet your job rules ({}), so this wasn't applied to."
RULES_UNCLEAR = "The posting doesn't say where or how the work is done, so this wasn't applied to."
_RULED_OUT = re.compile(r"\b(not|no|non|never|without|isn't|aren't|can be|could be|may be)\b[^.\n]{0,25}$", re.I)
_SIGN_OFF = re.compile(
    r"\n\s*(kind regards|best regards|warm regards|regards|best wishes|best|many thanks|thanks|"
    r"thank you|sincerely|yours sincerely|yours faithfully|cheers)\s*[,.!]?\s*(\n[^\n]{0,80}){0,5}\s*$",
    re.I,
)


def _env_int(name, default):
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return default
    return value if value >= 0 else default


def min_score():
    return _env_int("MIN_MATCH_SCORE", 60)


def daily_limit():
    return _env_int("DAILY_SEND_LIMIT", 25)


def crawl_max_jobs():
    return _env_int("CRAWL_MAX_JOBS", 15)


def sent_today():
    midnight = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    return Application.objects.filter(status=Status.SENT, sent_at__gte=midnight).count()


def missing_setup(need_gmail=True):
    """What the user still has to do before applications can run."""
    missing = []
    if not Resume.objects.exists():
        missing.append("upload your CV")
    elif not Profile.load().full_name:
        missing.append("add your name")
    if need_gmail and not mailer.is_configured():
        missing.append("connect Gmail")
    if not llm.api_key():
        missing.append("add your Gemini API key")
    return missing


def office_wording(text):
    """The phrase saying a job needs the office, or "" if there's none. "Not a hybrid role" doesn't count."""
    text = text or ""
    for match in _OFFICE_WORK.finditer(text):
        if not _RULED_OUT.search(text[max(0, match.start() - 30): match.start()]):
            return " ".join(match.group().split()).strip(" ()[]|/,:\u2013\u2014-")
    return ""


def split_places(text):
    return [place.strip() for place in (text or "").split(",") if place.strip()]


def _mentions(places, text):
    return any(re.search(rf"\b{re.escape(place)}\b", text or "", re.I) for place in split_places(places))


def _rule_problem(profile, job_text):
    """What the posting's own words rule out, however Gemini read it. Only places you listed allow office days."""
    if _mentions(profile.onsite_places, job_text):
        return ""
    office = office_wording(job_text)
    return f'The posting says "{office}"' if office else ""


def facts(profile):
    """What the user has said about themselves. Until they edit it, the starting text applies."""
    return profile.facts or prompts.FACTS_TEMPLATE


def rules(profile):
    return prompts.rules(profile.onsite_places, profile.remote_countries)


def _cv(cv, cv_chars=CV_CHARS):
    return {"id": str(cv.pk), "cv": cv.text[:cv_chars]}


def spawn(fn, *args):
    """Runs slow work (Gemini, SMTP, crawling) off the request thread."""

    def run():
        try:
            fn(*args)
        except Exception:
            log.exception("Background task %s crashed", fn.__name__)
        finally:
            connections.close_all()

    threading.Thread(target=run, daemon=True).start()


def recover_interrupted():
    """Work is done in this process, so anything in flight from before it started is dead."""
    stale = {"updated_at__lt": BOOT_TIME}
    Application.objects.filter(status__in=[Status.QUEUED, Status.PROCESSING], **stale).update(
        status=Status.FAILED, note="Interrupted when the app stopped. Click Retry."
    )
    Application.objects.filter(status=Status.SENDING, **stale).update(status=Status.FAILED, note=INTERRUPTED_SEND)
    Application.objects.filter(status=Status.FILLING, **stale).update(status=Status.NEEDS_YOU, note=INTERRUPTED_FORM)
    CrawlRun.objects.filter(status=CrawlRun.Status.RUNNING, **stale).update(
        status=CrawlRun.Status.FAILED, note="Interrupted when the app stopped."
    )


# ---------------------------------------------------------------- CV


def save_resume(name, data):
    """Reads a CV and stores it, without Gemini unless it's a scan. Raises ResumeError with a readable message.
    A CV with the same file name replaces the old one. Your details are only filled in where they're blank."""
    text = resume.extract_text(name, data)
    if len(text) < resume.MIN_TEXT and resume.is_pdf(name):
        text = _read_scanned_pdf(data)
    if len(text) < resume.MIN_TEXT:
        raise resume.ResumeError("Couldn't find enough text in that file to read it as a CV.")
    name = Path(name).name[-200:]  # the end of the name holds the file type
    cv = Resume.objects.filter(name=name).first()
    if not cv and Resume.objects.count() >= Resume.MAX_COUNT:
        raise resume.ResumeError(f"You can keep up to {Resume.MAX_COUNT} CVs. Remove one first.")
    cv = cv or Resume(name=name)
    cv.data, cv.text = data, text
    cv.save()
    details = resume.guess_contact(text)
    profile = Profile.load()
    profile.full_name = profile.full_name or details["full_name"][:120]
    profile.phone = profile.phone or details["phone"][:40]
    profile.linkedin = profile.linkedin or details["linkedin"][:200]
    profile.save()
    return cv


def _read_scanned_pdf(data):
    if not llm.api_key():
        raise resume.ResumeError("That PDF looks like a scan. Add your Gemini key first so it can be read.")
    try:
        return _text(llm.generate_json(prompts.OCR_PROMPT, prompts.OCR_SCHEMA, pdf=data, patient=False).get("text"))
    except llm.LLMError as exc:
        raise resume.ResumeError(f"That PDF looks like a scan and Gemini couldn't read it: {exc}") from exc


def attachment_name(profile, cv):
    stem = re.sub(r"[^\w-]+", "_", profile.full_name).strip("_") or "Candidate"
    return f"{stem}_CV{Path(cv.name).suffix.lower()}"


# ---------------------------------------------------------------- Intake


def source_hash(value):
    return hashlib.sha256(" ".join(value.lower().split()).encode()).hexdigest()


def _claim(digest, **fields):
    """One row per job, so the same job can't be processed twice. Failed ones may try again."""
    app, created = Application.objects.get_or_create(source_hash=digest, defaults=fields)
    if created:
        return app, True
    reset = (
        Application.objects.filter(pk=app.pk, status=Status.FAILED)
        .exclude(note=INTERRUPTED_SEND)  # it may already have gone out, so the user decides on the row itself
        .update(status=Status.QUEUED, note="", updated_at=timezone.now(), **fields)
    )
    if reset:
        app.refresh_from_db()
    return app, bool(reset)


def submit_jd(text, auto_send):
    """Returns (application, is_new). The caller runs process() in the background if new."""
    return _claim(
        source_hash(text),
        source=Source.PASTE,
        jd_text=text[:STORED_JD_CHARS],
        candidates=emails.find_emails(text, exclude=mailer.address()),
        auto_send=auto_send,
    )


# ---------------------------------------------------------------- Tailoring


def _text(value):
    """Gemini's value as text. Half an emoji (a lone surrogate) can't be stored, so it's dropped."""
    return str(value).encode("utf-8", "ignore").decode().strip() if value is not None else ""


def _clean_body(body, name):
    body = body.replace("\r\n", "\n").replace("**", "").strip()
    body = re.sub(r"\n\s*\n", "\n\n", body)
    body = _SIGN_OFF.sub("", body).rstrip()
    if name and body.lower().endswith(name.lower()):
        body = body[: -len(name)].rstrip(" ,\n")
    return body


def _signature(profile):
    contact = " | ".join(part for part in (profile.phone, profile.linkedin) if part)
    return "\n".join(line for line in ("Kind regards,", profile.full_name, contact) if line)


def _check_job(raw, profile):
    """Coerces Gemini's answer into safe values. Returns (job, problem)."""
    try:
        score = max(0, min(100, int(raw.get("match_score"))))
    except (TypeError, ValueError):
        score = 0
    rules = _text(raw.get("rules_met")).lower()
    job = {
        "is_job_posting": raw.get("is_job_posting") is True,
        "title": _text(raw.get("title"))[:300],
        "company": _text(raw.get("company"))[:200],
        "location": _text(raw.get("location"))[:200],
        "rules_met": "no" if re.match(r"no\b", rules) else "yes" if re.match(r"yes\b", rules) else "unclear",
        "match_score": score,
        "reason": _text(raw.get("reason"))[:500],
        "application_email": _text(raw.get("application_email")),
    }
    subject = " ".join(_text(raw.get("subject")).split())[:300]
    body = _clean_body(_text(raw.get("body")), profile.full_name)
    placeholder = _PLACEHOLDER.search(body) or _PLACEHOLDER.search(subject)
    too_short = len(body.split()) < MIN_BODY_WORDS
    if not job["is_job_posting"] and (too_short or placeholder):
        job.update(subject=subject, body="")  # not a job, so no email is expected
        return job, ""
    if too_short:
        return job, "the email was too short"
    fallback = f"Application for {job['title'] or 'your vacancy'} - {profile.full_name}"
    job.update(subject=subject or fallback[:300], body=f"{body}\n\n{_signature(profile)}")
    if placeholder:
        return job, f'the email contains what looks like a placeholder: "{placeholder.group()}"'
    return job, ""


def _tailor(app, profile, cvs):
    """One call, however many CVs: Gemini picks the best fit and writes the email from it."""
    base = prompts.tailor_prompt(
        profile.full_name,
        rules(profile),
        facts(profile),
        [_cv(cv) for cv in cvs],
        app.jd_text[:JD_CHARS],
        app.candidates,
        timezone.localdate().isoformat(),
    )
    prompt = base
    for _ in range(2):
        raw = llm.generate_json(prompt, prompts.TAILOR_SCHEMA, system=prompts.TAILOR_SYSTEM)
        chosen = _text(raw.get("cv_id"))
        job, problem = _check_job(raw, profile)
        job["resume"] = next((cv for cv in cvs if str(cv.pk) == chosen), cvs[0])
        if not problem:
            return job
        prompt = f"{base}\n\nYour previous draft was rejected because {problem}. Follow the rules exactly."
    if job.get("body"):  # a flagged draft is still worth keeping, but it waits for the user
        job["review"] = f"Please check this draft: {problem}."
        return job
    raise llm.LLMError(f"Gemini's draft failed the quality check: {problem}. Click Retry.")


def _decide(app, job, profile):
    """Returns (status, note) for a tailored application."""
    crawled = app.source == Source.CRAWL
    if not job["is_job_posting"]:
        return Status.SKIPPED, "This doesn't look like an open job posting."
    # Your job rules hold for pasted jobs too. Only the user pressing Send can overrule them.
    if job["rules_met"] == "no":
        return Status.SKIPPED, RULES_NOT_MET.format(profile.rule_summary)
    problem = _rule_problem(profile, app.jd_text)
    if problem:
        return Status.SKIPPED, f"{problem}, so this wasn't applied to."
    if job["rules_met"] != "yes":
        return Status.SKIPPED, RULES_UNCLEAR
    if crawled and job["match_score"] < min_score():
        return Status.SKIPPED, f"Match score {job['match_score']} is below your minimum of {min_score()}."
    if not app.recipient:
        if crawled and app.auto_send:
            return Status.FORM_QUEUE, ""
        if crawled:
            return Status.MANUAL, "This posting has no application email. Fill the form, or open the link and use the message below."
        return Status.READY, "No application email found. Add one below."
    earlier = (
        Application.objects.filter(status=Status.SENT, recipient__iexact=app.recipient, title__iexact=app.title)
        .exclude(pk=app.pk)
        .first()
        if app.title
        else None
    )
    if earlier:
        when = timezone.localtime(earlier.sent_at).strftime("%-d %b %Y")
        return Status.SKIPPED, f"You already applied to {app.recipient} for this role on {when}."
    return Status.READY, ""


def process(app_id):
    """Tailors one application, then sends it if allowed. Never leaves the row stuck."""
    claimed = Application.objects.filter(pk=app_id, status=Status.QUEUED).update(
        status=Status.PROCESSING, updated_at=timezone.now()
    )
    if not claimed:
        return
    app = Application.objects.get(pk=app_id)
    named = ""
    try:
        cvs = list(Resume.objects.all())
        if not cvs:
            raise llm.LLMError("Upload your CV first, then click Retry.")
        profile = Profile.load()
        job = _tailor(app, profile, cvs)
        app.resume = job["resume"]
        app.title = job["title"]
        app.company = job["company"]
        app.location = job["location"]
        app.rules_met = job["rules_met"]
        app.score = job["match_score"]
        app.reason = job["reason"]
        app.subject = job["subject"]
        app.body = job["body"]
        # Only an address Gemini names goes out by itself. On a pasted job the best guess is
        # filled in for review, because the only address may be a footer's privacy contact.
        named = emails.choose_recipient(app.candidates, job["application_email"], app.jd_text, strict=True)
        crawled = app.source == Source.CRAWL
        app.recipient = named or emails.choose_recipient(app.candidates, "", app.jd_text, strict=crawled)
        app.status, app.note = _decide(app, job, profile)
        if job.get("review"):
            app.auto_send = False
            app.status = Status.MANUAL if app.status == Status.FORM_QUEUE else app.status
            app.note = f"{app.note} {job['review']}".strip()
        elif app.status == Status.READY and app.auto_send and app.recipient and not named:
            app.note = UNCONFIRMED_ADDRESS
        app.save()
    except llm.LLMError as exc:
        _fail(app, str(exc))
        return
    except Exception as exc:
        log.exception("Processing application %s failed", app_id)
        _fail(app, f"Unexpected error: {exc}")
        return
    if app.status == Status.READY and app.auto_send and named:
        send_application(app.pk)
    elif app.status == Status.FORM_QUEUE:
        ensure_form_worker()


def _fail(app, note):
    # Not save(): the failure may have come from a value on this row that can't be stored.
    Application.objects.filter(pk=app.pk).update(status=Status.FAILED, note=note, updated_at=timezone.now())
    app.status, app.note = Status.FAILED, note


def retry(app_id):
    """Runs tailoring again. Pages that never loaded are fetched first."""
    app = Application.objects.get(pk=app_id)
    if app.source == Source.CRAWL and not app.jd_text and app.job_url:
        try:
            page = crawler.Crawler().fetch(app.job_url)
        except crawler.CrawlError as exc:
            _fail(app, str(exc))
            return
        except Exception as exc:
            log.exception("Loading page for application %s failed", app_id)
            _fail(app, f"Unexpected error: {exc}")
            return
        app.jd_text = page.text[:STORED_JD_CHARS]
        app.candidates = [a for a in page.emails if a != mailer.address().lower()]
        app.save()
    process(app_id)


# ---------------------------------------------------------------- Sending


def _send_problem(app, cv):
    if not app.recipient:
        return "Add a recipient email first."
    try:
        validate_email(app.recipient)
    except ValidationError:
        return f"{app.recipient} isn't a valid email address."
    if not app.body.strip():
        return "There's no email to send yet."
    if not cv:
        return "The CV chosen for this job was removed. Click Rewrite with Gemini to pick another."
    if not mailer.is_configured():
        return "Connect Gmail first."
    limit = daily_limit()
    if sent_today() >= limit:
        return f"Daily limit of {limit} emails reached, so this is saved for later. Raise DAILY_SEND_LIMIT in .env to send more."
    return ""


def claim_for_sending(app_id, **edits):
    """Marks the row as sending, once. A double click or a retry can't send twice."""
    return bool(
        Application.objects.filter(pk=app_id)
        .exclude(status__in=[Status.SENT, *IN_FLIGHT])
        .update(status=Status.SENDING, updated_at=timezone.now(), channel=Application.Channel.EMAIL, **edits)
    )


def send_application(app_id):
    if claim_for_sending(app_id):
        deliver(app_id)


def deliver(app_id):
    """Sends a row already claimed with claim_for_sending()."""
    with _send_lock:
        app = Application.objects.get(pk=app_id)
        try:
            profile, cv = Profile.load(), app.resume
            problem = _send_problem(app, cv)
            if problem:
                app.status, app.note = Status.READY, problem
                app.save()
                return
            mailer.send(
                to=app.recipient,
                subject=app.subject or "Job application",
                body=app.body,
                from_name=profile.full_name,
                attachment_name=attachment_name(profile, cv),
                attachment=bytes(cv.data),
                mimetype=resume.mimetype(cv.name),
            )
        except mailer.MailError as exc:
            _fail(app, str(exc))
        except Exception as exc:
            log.exception("Sending application %s failed", app_id)
            _fail(app, f"Unexpected error while sending: {exc}")
        else:
            app.status = Status.SENT
            app.note = ""
            app.sent_at = timezone.now()
            app.save()


# ---------------------------------------------------------------- Website forms


HEADLESS = False  # visible on purpose: you can see what's being submitted in your name
_worker_lock = threading.Lock()
_worker_running = False


def ensure_form_worker():
    """One worker fills forms, one at a time. Also restarts queued work after the app restarts."""
    global _worker_running
    if not Application.objects.filter(status=Status.FORM_QUEUE).exists():
        return
    with _worker_lock:
        if _worker_running:
            return
        _worker_running = True
    spawn(_form_worker)


def _form_worker():
    global _worker_running
    try:
        with formfill.Chrome(headless=HEADLESS) as chrome:
            while True:
                app = Application.objects.filter(status=Status.FORM_QUEUE).order_by("updated_at").first()
                if app:
                    _fill_form(app, chrome)
                elif chrome.windows_open():
                    chrome.wait(1500)  # stays alive so windows left for the user aren't closed
                else:
                    connections.close_all()  # Django opens a separate connection inside Playwright's loop
                    break
    except Exception as exc:  # e.g. Chrome couldn't start, so nothing queued can proceed
        if not isinstance(exc, formfill.FormError):
            log.exception("The form worker stopped")
        Application.objects.filter(status=Status.FORM_QUEUE).update(
            status=Status.MANUAL, note=str(exc)[:300], updated_at=timezone.now()
        )
    finally:
        with _worker_lock:
            _worker_running = False


def _form_job(app, profile, cv, submit, stay_open):
    message = app.body
    stem = attachment_name(profile, cv).split("_CV")[0]
    return formfill.Job(
        url=app.job_url,
        full_name=profile.full_name,
        email=mailer.address(),
        phone=profile.phone,
        linkedin=profile.linkedin,
        message=message,
        resume_name=attachment_name(profile, cv),
        resume_bytes=bytes(cv.data),
        cover_name=f"{stem}_Cover_Letter.docx",
        cover_bytes=resume.letter_as_docx(message),
        allow_consent=profile.allow_consent,
        submit=submit,
        stay_open=stay_open,
        ask=lambda fields: _ask_gemini(app, profile, cv, fields),
    )


def _ask_gemini(app, profile, cv, fields):
    try:
        raw = llm.generate_json(
            prompts.form_prompt(profile.full_name, cv.text[:CV_CHARS], facts(profile), app.jd_text[:JD_CHARS], fields),
            prompts.FORM_SCHEMA,
            system=prompts.FORM_SYSTEM,
        )
    except llm.LLMError as exc:
        raise formfill.FormError(str(exc)) from exc
    replies = {}
    answers = raw.get("answers")
    for answer in answers if isinstance(answers, list) else []:
        if isinstance(answer, dict) and isinstance(answer.get("values"), list):
            replies[_text(answer.get("id"))] = formfill.Answer(
                [_text(value) for value in answer["values"]], _text(answer.get("source")).lower()
            )
    return replies


def _fill_form(app, chrome):
    claimed = Application.objects.filter(pk=app.pk, status=Status.FORM_QUEUE).update(
        status=Status.FILLING, note="", updated_at=timezone.now()
    )
    if not claimed:
        return
    app.refresh_from_db()
    profile, cv = Profile.load(), app.resume
    # auto_send off means the user pressed the button and is watching: they press Submit themselves.
    under_limit = sent_today() < daily_limit()
    submit = app.auto_send and under_limit
    try:
        if not cv:
            outcome = formfill.Outcome(
                formfill.NEEDS_YOU, "The CV chosen for this job was removed. Click Rewrite with Gemini to pick another."
            )
        elif crawler.Crawler().allowed(app.job_url):
            outcome = formfill.apply(chrome, _form_job(app, profile, cv, submit, stay_open=not app.auto_send))
        else:
            outcome = formfill.Outcome(
                formfill.NEEDS_YOU, "This site asks automated tools not to open this page (robots.txt). Open the link yourself."
            )
    except Exception as exc:
        log.exception("Filling the form for application %s failed", app.pk)
        outcome = formfill.Outcome(formfill.NEEDS_YOU, f"Unexpected error: {exc}")

    fields = {"form_answers": outcome.answers, "note": _text(outcome.note), "updated_at": timezone.now()}
    if outcome.state == formfill.SUBMITTED:
        fields.update(status=Status.SENT, channel=Application.Channel.FORM, sent_at=timezone.now())
    elif outcome.state == formfill.NO_FORM:
        fields.update(status=Status.MANUAL, note=f"{outcome.note} Open the link and use the message below.")
    else:
        fields.update(status=Status.NEEDS_YOU)
        if app.auto_send and not under_limit:
            fields["note"] = "Daily limit reached, so this form wasn't submitted. Press Fill the form to finish it yourself."
    Application.objects.filter(pk=app.pk).update(**fields)


def queue_form(app_id):
    """The user pressed Fill the form: it fills in, then waits for them to press Submit."""
    claimed = (
        Application.objects.filter(pk=app_id)
        .exclude(status__in=[Status.SENT, *IN_FLIGHT])
        .update(status=Status.FORM_QUEUE, auto_send=False, note="", updated_at=timezone.now())
    )
    if claimed:
        ensure_form_worker()
    return bool(claimed)


def mark_applied(app_id):
    """The user finished the application themselves. Counts towards the daily limit like any other."""
    return bool(
        Application.objects.filter(pk=app_id, status__in=[Status.NEEDS_YOU, Status.MANUAL]).update(
            status=Status.SENT,
            channel=Application.Channel.FORM,
            sent_at=timezone.now(),
            note="You marked this as submitted.",
            updated_at=timezone.now(),
        )
    )


# ---------------------------------------------------------------- Crawling


def _job_urls(page, profile, cvs):
    limit = crawl_max_jobs()
    remote_only = not split_places(profile.onsite_places)
    triage = llm.generate_json(
        prompts.triage_prompt(rules(profile), [_cv(cv, TRIAGE_CHARS) for cv in cvs], page.text[:TRIAGE_CHARS], page.links, limit),
        prompts.TRIAGE_SCHEMA,
        system=prompts.TRIAGE_SYSTEM,
    )
    if triage.get("page_type") == "single_job":
        return [page.url]
    urls = []
    picks = triage.get("job_links")
    for number in picks if isinstance(picks, list) else []:
        if isinstance(number, int) and not isinstance(number, bool) and 1 <= number <= len(page.links):
            url, label = page.links[number - 1]
            if url not in urls and not (remote_only and office_wording(label)):
                urls.append(url)
    return urls[:limit]


def _crawl(run):
    profile, cvs = Profile.load(), list(Resume.objects.all())
    fetcher = crawler.Crawler()
    first_page = fetcher.fetch(run.url)
    urls = _job_urls(first_page, profile, cvs)
    if not urls:  # some boards only list their jobs once their JavaScript has run
        first_page = fetcher.fetch_rendered(first_page.url)
        urls = _job_urls(first_page, profile, cvs)
    if not urls:
        run.status = CrawlRun.Status.DONE
        run.note = f"Found no matching jobs on that page. If it needs a login, try another link. {crawler.PASTE_INSTEAD}"
        return
    run.total = len(urls)
    run.save()

    own = mailer.address().lower()
    fails = already_seen = 0
    for url in urls:
        app, is_new = _claim(
            source_hash(url.rstrip("/")),
            source=Source.CRAWL,
            crawl=run,
            job_url=url[:2000],
            auto_send=run.auto_send,
            jd_text="",
            candidates=[],
        )
        if not is_new:
            already_seen += 1
        else:
            _load_and_process(app, url, first_page, fetcher, own)
            app.refresh_from_db()
            fails = fails + 1 if app.status == Status.FAILED else 0
        run.done += 1
        run.save()
        if fails >= MAX_FAILS_IN_A_ROW:
            run.status = CrawlRun.Status.FAILED
            run.note = f"Stopped after {fails} failures in a row. Last problem: {app.note}"
            return

    counts = {status: run.applications.filter(status=status).count() for status in Status.values}
    summary = (
        f"{counts[Status.SENT]} sent, {counts[Status.READY]} ready to send, "
        f"{counts[Status.FORM_QUEUE] + counts[Status.FILLING]} going through website forms, "
        f"{counts[Status.NEEDS_YOU] + counts[Status.MANUAL]} to apply manually, "
        f"{counts[Status.SKIPPED]} skipped, {counts[Status.FAILED]} failed"
    )
    run.status = CrawlRun.Status.DONE
    run.note = f"{summary}, {already_seen} seen before." if already_seen else f"{summary}."


def _load_and_process(app, url, first_page, fetcher, own):
    try:
        page = first_page if url == first_page.url else fetcher.fetch(url)
        app.jd_text = page.text[:STORED_JD_CHARS]
        app.candidates = [address for address in page.emails if address != own]
        app.save()
    except crawler.CrawlError as exc:
        _fail(app, str(exc))
        return
    except Exception:  # one odd page must not stop the jobs after it
        log.exception("Reading %s failed", url)
        _fail(app, f"Couldn't read that page. {crawler.PASTE_INSTEAD}")
        return
    process(app.pk)


def run_crawl(run_id):
    run = CrawlRun.objects.get(pk=run_id)
    try:
        _crawl(run)
    except (crawler.CrawlError, llm.LLMError) as exc:
        run.status = CrawlRun.Status.FAILED
        run.note = str(exc)
    except Exception as exc:
        log.exception("Crawl %s failed", run_id)
        run.status = CrawlRun.Status.FAILED
        run.note = f"Unexpected error: {exc}"
    run.save()
