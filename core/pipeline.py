"""The workflow: read a job, tailor an application, decide what to do, send it."""
import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import connections
from django.db.models import Q
from django.utils import timezone

from . import countries, crawler, emails, formfill, jobtext, llm, mailer, prompts, resume
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
# A sign-off on a line of its own, or after the last sentence on its line: "...your team. Best regards,"
_SIGN_OFF = re.compile(
    r"(?:\n\s*|(?<=[.!?])[ \t]+)(kind regards|best regards|warm regards|regards|best wishes|best|many thanks|thanks|"
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
    unnamed = Resume.objects.filter(full_name="").values_list("name", flat=True).first()
    if not Resume.objects.exists():
        missing.append("upload your CV")
    elif unnamed:
        missing.append(f"add your name to {unnamed}")
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


def facts(cv):
    """What the user has said about themselves for jobs that get this CV. Until they edit it, the starting text applies."""
    return cv.facts or prompts.FACTS_TEMPLATE


def own_addresses():
    """Your Gmail accounts and every CV's email address. None of them is ever a recipient."""
    return [*mailer.accounts(), *Resume.objects.exclude(email="").values_list("email", flat=True)]


def sender(cv):
    """The connected Gmail account this CV's emails go out from: the one chosen for it in Setup, else the one with the
    CV's own address, else your only account. With several and none of those, "" rather than another CV's Gmail."""
    accounts = mailer.accounts()
    return mailer.account(cv.send_from) or mailer.account(cv.email) or (accounts[0] if len(accounts) == 1 else "")


def spelling(cv):
    return "US English" if countries.same(cv.country, "United States") else "UK English"


def rules(profile):
    return prompts.rules(profile.onsite_places, profile.remote_countries)


def _cv(cv, cv_chars=CV_CHARS):
    return {"id": str(cv.pk), "name": cv.full_name, "country": cv.country, "cv": cv.text[:cv_chars], "facts": facts(cv)}


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
    """Reads a CV and stores it with who it says you are: name, email, phone, LinkedIn, links and country. No Gemini
    unless it's a scan. Raises ResumeError with a readable message. A CV with the same file name replaces the old
    one, keeping its facts, its Gmail account and anything you typed, and cv.replaced says so."""
    text = resume.extract_text(name, data)
    if len(text) < resume.MIN_TEXT and resume.is_pdf(name):
        text = _read_scanned_pdf(data)
    if len(text) < resume.MIN_TEXT:
        raise resume.ResumeError("Couldn't find enough text in that file to read it as a CV.")
    name = Path(name).name[-200:]  # the end of the name holds the file type
    cv = Resume.objects.filter(name=name).first()
    if not cv and Resume.objects.count() >= Resume.MAX_COUNT:
        raise resume.ResumeError(f"You can keep up to {Resume.MAX_COUNT} CVs. Remove one first.")
    found = resume.contact_details(name, data, text)
    keep = dict.fromkeys(_SIZES, False)
    if cv:  # a new version: a detail you typed or cleared stays, and one the old file gave makes way
        before = resume.contact_details(cv.name, bytes(cv.data), cv.text)
        keep = {key: getattr(cv, key) != before[key][:size] for key, size in _SIZES.items()}
    old = {key: getattr(cv, key) for key in ("full_name", "phone", "linkedin")} if cv else None
    cv = cv or Resume(name=name)
    cv.replaced = old is not None
    cv.data, cv.text = data, text
    for key, size in _SIZES.items():  # jobs that get this CV go out with its own details
        if not keep[key]:
            setattr(cv, key, found[key][:size])
    cv.save()
    if old:
        resign_drafts(cv, old["full_name"], old["phone"], old["linkedin"])
    return cv


_SIZES = {"full_name": 120, "email": 254, "phone": 40, "linkedin": 200, "links": 10_000, "country": 60}


def _read_scanned_pdf(data):
    if not llm.api_key():
        raise resume.ResumeError("That PDF looks like a scan. Add your Gemini key first so it can be read.")
    try:
        return _text(llm.generate_json(prompts.OCR_PROMPT, prompts.OCR_SCHEMA, pdf=data, patient=False).get("text"))
    except llm.LLMError as exc:
        raise resume.ResumeError(f"That PDF looks like a scan and Gemini couldn't read it: {exc}") from exc


def attachment_name(cv):
    stem = re.sub(r"[^\w-]+", "_", cv.full_name).strip("_") or "Candidate"
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
        candidates=emails.find_emails(text, exclude=own_addresses()),
        auto_send=auto_send,
    )


# ---------------------------------------------------------------- Tailoring


def _text(value):
    """Gemini's value as text. Half an emoji (a lone surrogate) can't be stored, so it's dropped."""
    return str(value).encode("utf-8", "ignore").decode().strip() if value is not None else ""


def _clean_body(body, name):
    body = body.replace("\r\n", "\n").replace("**", "").strip()
    body = re.sub(r"\n\s*\n", "\n\n", body)
    for _ in range(2):  # the name can come after the sign-off on its own line: "Best regards, Sam Taylor"
        body = _SIGN_OFF.sub("", body).rstrip()
        if name and body.lower().endswith(name.lower()):
            body = body[: -len(name)].rstrip(" ,\n")
    return body


def _contact(phone, linkedin):
    return " | ".join(part for part in (phone, linkedin) if part)


def _sign(name, phone, linkedin):
    return "\n".join(line for line in ("Kind regards,", name, _contact(phone, linkedin)) if line)


def _signature(cv):
    return _sign(cv.full_name, cv.phone, cv.linkedin)


def resign_drafts(cv, old_name, old_phone, old_linkedin):
    """Emails not yet sent with this CV were signed with its old name, phone and LinkedIn, so they take the new ones,
    unless you've changed how a draft ends. A draft you signed with another name keeps it, a draft written while the
    CV had no name gets one, and a subject ending in the old name, as the app writes them, takes the new one. A draft
    already typed into a website form is left as it went. Returns how many changed."""
    old, new = _contact(old_phone, old_linkedin), _contact(cv.phone, cv.linkedin)
    renamed = bool(cv.full_name) and old_name != cv.full_name
    if old == new and not renamed:
        return 0
    ending = re.compile(r"\n\nKind regards,(?:\n([^\n]+))?" + (re.escape(f"\n{old}") if old else "") + r"\Z")
    done = [Status.SENT, Status.SENDING, Status.FILLING]  # on their way out, or gone: what was sent stays on record
    changed = 0
    drafts = (
        Application.objects.filter(resume=cv, form_answers=[])  # once a form is filled in, the message is in it
        .exclude(status__in=done)
        .exclude(note__in=[INTERRUPTED_SEND, INTERRUPTED_FORM, *formfill.MAYBE_SUBMITTED])  # may have gone out as they are
        .only("pk", "body", "subject")
    )
    for app in drafts:
        match = ending.search(app.body)
        if not match:
            continue
        signed_as = match.group(1) or ""
        name = cv.full_name if renamed and signed_as == old_name else signed_as
        body = f"{app.body[: match.start()]}\n\n{_sign(name, cv.phone, cv.linkedin)}"
        subject = app.subject
        if renamed and name == cv.full_name and re.search(rf" -\s*{re.escape(old_name)}$", subject):
            subject = re.sub(rf" -\s*{re.escape(old_name)}$", f" - {cv.full_name}", subject)[:300]
        if (body, subject) != (app.body, app.subject):
            changed += (
                Application.objects.filter(pk=app.pk, body=app.body, subject=app.subject)
                .exclude(status__in=done)
                .update(body=body, subject=subject)
            )
    return changed


def _check_job(raw, cv):
    """Coerces Gemini's answer into safe values, signed with the chosen CV's details. Returns (job, problem)."""
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
    body = _clean_body(_text(raw.get("body")), cv.full_name)
    placeholder = _PLACEHOLDER.search(body) or _PLACEHOLDER.search(subject)
    too_short = len(body.split()) < MIN_BODY_WORDS
    if not job["is_job_posting"] and (too_short or placeholder):
        job.update(subject=subject, body="")  # not a job, so no email is expected
        return job, ""
    if too_short:
        return job, "the email was too short"
    fallback = f"Application for {job['title'] or 'your vacancy'} - {cv.full_name}"
    job.update(subject=subject or fallback[:300], body=f"{body}\n\n{_signature(cv)}")
    if placeholder:
        return job, f'the email contains what looks like a placeholder: "{placeholder.group()}"'
    return job, ""


def _reach(raw, cvs, job_text, onsite_places):
    """Which CVs a job may go out with. Gemini reads the posting, but the code decides.

    - A job that isn't clearly fully remote only goes with a CV for a country where your rules allow on-site work,
      and is turned down if it's anywhere else.
    - A remote job limited to some countries only goes with a CV for one of them, and waits for you if there's none.
    - A remote job the posting opens to anyone anywhere may go with any CV, and with one per country, unless the
      employer is in a country where your rules allow on-site work: then only that country's CVs, as for its office.
    - Otherwise the CV for the employer's country if you have one, else the best match.

    CVs without a country only stand in when none of your CVs has one. Returns allowed (CVs, in your order), scores
    ({pk: match score}), several (more than one may go), remote, anywhere, label (where it's open to, for the page),
    refuse and hold (notes for turning it down or waiting for you, or "")."""
    setup = _text(raw.get("work_setup")).lower()
    remote = "remote" in setup and not re.search(r"hybrid|on-?site|office", setup) and not office_wording(job_text)
    limited = raw.get("limited_to") if isinstance(raw.get("limited_to"), list) else []
    limited = list(dict.fromkeys(country for name in limited for country in countries.resolve(_text(name))))
    location = _text(raw.get("location"))
    employer_in = countries.resolve(_text(raw.get("based_in")))
    based = employer_in[0] if len(employer_in) == 1 else countries.only_place(location)
    onsite = list(dict.fromkeys(countries.canonical(place) for place in split_places(onsite_places)))
    onsite_keys = {countries.key(place) for place in onsite}

    def cvs_for(places):
        keys = {countries.key(place) for place in places}
        return [cv for cv in cvs if cv.country and countries.key(cv.country) in keys]

    anywhere = remote and raw.get("open_to_anywhere") is True and not limited
    employer = f"with an employer in {based}" if based else ""
    refuse = hold = ""
    if anywhere and based and countries.key(based) in onsite_keys and cvs_for([based]):
        label, pool = f"Open to people anywhere, {employer}", cvs_for([based])  # that country's job, as for its office
        anywhere = False
    elif not remote:  # it's open where it is
        where = list(dict.fromkeys(country for _, country in countries.places(location)))
        place = limited or where or ([based] if based else []) or [place for place in onsite if _mentions(place, job_text)]
        inside = [name for name in place if countries.key(name) in onsite_keys]
        label = f"In person in {', '.join(place)}" if place else "In person"
        if place and not inside:
            allowed_in = f", but your rules only allow on-site or hybrid work in {onsite_places}" if onsite_places else ""
            refuse = f"This job isn't fully remote and is in {', '.join(place)}{allowed_in}, so this wasn't applied to."
        pool = cvs_for(inside or onsite)
        if not pool and any(cv.country for cv in cvs) and not refuse:
            there = ", ".join(inside or onsite) or "a place your rules allow"
            hold = f"This job is in person in {there}, and none of your CVs is for there, so it waits for you."
    elif limited:
        label, pool = f"Open to people in {', '.join(limited)}", cvs_for(limited)
        if not pool and any(cv.country for cv in cvs):
            hold = f"This job is only open to people in {', '.join(limited)}, and none of your CVs is for there, so it waits for you."
    elif anywhere:
        label, pool = "Open to people anywhere", list(cvs)
    else:
        label, pool = f"Remote, {employer or 'with no stated limit'}", cvs_for([based]) or list(cvs)
    if not pool:  # you don't sort your CVs by country, or the job waits for you: any may draft it
        pool = list(cvs)
    scores = {}
    for entry in raw.get("cv_scores") if isinstance(raw.get("cv_scores"), list) else []:
        try:
            scores[_text(entry.get("cv_id"))] = max(0, min(100, int(entry.get("match_score"))))
        except (AttributeError, TypeError, ValueError):
            continue
    return {
        "allowed": pool,
        "scores": {cv.pk: scores.get(str(cv.pk), 0) for cv in cvs},
        "several": anywhere and len({countries.key(cv.country) for cv in pool if cv.country}) > 1,
        "remote": remote,
        "anywhere": anywhere,
        "label": label[:300],
        "refuse": refuse,
        "hold": hold,
    }


def _tailor(app, profile, cvs, fixed=False):
    """Gemini reads where the job is open to and how well each CV fits, and writes the email from the CV it picks.
    The job gets the best-scoring CV it may go with, and if that isn't the one Gemini wrote from, the email is written
    again with only that CV in front of it. fixed: cvs is the one CV a further application keeps, and where the job
    is open to is judged against all your CVs, as it was for the first."""

    def ask(options, problem=""):
        prompt = prompts.tailor_prompt(
            rules(profile),
            [_cv(cv) for cv in options],
            app.jd_text[:JD_CHARS],
            app.candidates,
            timezone.localdate().isoformat(),
        )
        if problem:
            prompt += f"\n\nYour previous draft was rejected because {problem}. Follow the rules exactly."
        return llm.generate_json(prompt, prompts.TAILOR_SCHEMA, system=prompts.TAILOR_SYSTEM)

    def rewritten(again):  # where the job is open to and whether it fits the rules stay as first read
        return {**first, **{key: again.get(key) for key in ("match_score", "reason", "subject", "body")}}

    raw = first = ask(cvs)
    reach = _reach(raw, list(Resume.objects.all()) if fixed else cvs, app.jd_text, profile.onsite_places)
    picked = next((cv for cv in cvs if str(cv.pk) == _text(raw.get("cv_id"))), None)
    doomed = raw.get("is_job_posting") is not True or re.match(r"no\b", _text(raw.get("rules_met")).lower())
    if fixed or len(cvs) == 1:
        cv = cvs[0]
    else:
        best = max(reach["scores"][cv.pk] for cv in reach["allowed"])
        leaders = [cv for cv in reach["allowed"] if reach["scores"][cv.pk] == best]
        cv = picked if picked in leaders else leaders[0]  # the best-scoring CV the job may get, Gemini's pick on a tie
        if reach["refuse"] or doomed:
            cv = picked or cv  # it won't be applied to, so it isn't written again
        elif cv != picked:  # its email was written from another CV, so it's written again from this one
            raw = rewritten(ask([cv]))
    for attempt in range(2):
        try:  # you may have changed its details while Gemini was writing
            cv.refresh_from_db(fields=["full_name", "phone", "linkedin"])
        except Resume.DoesNotExist:
            raise llm.LLMError("The CV chosen for this job was removed while it was being written. Click Retry.") from None
        job, problem = _check_job(raw, cv)
        if not problem or attempt:
            break
        raw = rewritten(ask([cv], problem))
    job.update(resume=cv, reach=reach)
    if not problem:
        return job
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
    if job["reach"]["refuse"]:
        return Status.SKIPPED, job["reach"]["refuse"]
    if app.parent_id and not _still_fits(job):
        country = job["resume"].country or "no particular country"
        return Status.SKIPPED, f"Read again, this job only takes one CV, so the one for {country} wasn't sent."
    if job["rules_met"] != "yes":
        return Status.SKIPPED, RULES_UNCLEAR
    if (crawled or app.parent_id) and job["match_score"] < min_score():  # a further CV must earn its place too
        return Status.SKIPPED, f"Match score {job['match_score']} is below your minimum of {min_score()}."
    group = app.parent_id or app.pk  # this job's other CVs went on purpose
    sent_before = (  # the same job: the same reach too, as a job open to other places is another job
        Application.objects.filter(status=Status.SENT, title__iexact=app.title)
        .filter(Q(open_to="") | Q(open_to=app.open_to))
        .exclude(pk__in=[app.pk, group])
        .exclude(parent_id=group)
    )
    if not app.recipient:
        earlier = sent_before.filter(company__iexact=app.company).first() if crawled and app.title and app.company else None
        if earlier:  # the same job found again under another link
            when = timezone.localtime(earlier.sent_at).strftime("%-d %b %Y")
            return Status.SKIPPED, f"You already applied to {app.company} for this role on {when}."
        if crawled and app.auto_send:
            return Status.FORM_QUEUE, ""
        if crawled:
            return Status.MANUAL, "This posting has no application email. Fill the form, or open the link and use the message below."
        return Status.READY, "No application email found. Add one below."
    earlier = sent_before.filter(recipient__iexact=app.recipient).first() if app.title else None
    if earlier:
        when = timezone.localtime(earlier.sent_at).strftime("%-d %b %Y")
        return Status.SKIPPED, f"You already applied to {app.recipient} for this role on {when}."
    return Status.READY, ""


def _still_fits(job):
    """A further CV goes only while, read again, the job still takes a CV per country and this one is among them."""
    reach = job["reach"]
    return reach["several"] and job["resume"] in reach["allowed"]


def process(app_id):
    """Tailors one application, then sends it if allowed. Never leaves the row stuck."""
    claimed = Application.objects.filter(pk=app_id, status=Status.QUEUED).update(
        status=Status.PROCESSING, updated_at=timezone.now()
    )
    if not claimed:
        return
    app = Application.objects.get(pk=app_id)
    named, job, auto_send = "", None, app.auto_send
    try:
        profile = Profile.load()
        if app.parent_id:  # a further CV for a job keeps that CV
            if not app.resume:
                raise llm.LLMError("The CV for this application was removed.")
            if app.parent and app.parent.resume_id == app.resume_id:
                raise llm.LLMError("The first application for this job now uses this CV, so this one isn't needed.")
            job = _tailor(app, profile, [app.resume], fixed=True)
        else:
            cvs = list(Resume.objects.all())
            if not cvs:
                raise llm.LLMError("Upload your CV first, then click Retry.")
            taken = set(app.extras.exclude(status=Status.SKIPPED).values_list("resume", flat=True))
            cvs = [cv for cv in cvs if cv.pk not in taken]  # never two applications with one CV
            if not cvs:
                raise llm.LLMError("Each of your CVs already has an application for this job.")
            job = _tailor(app, profile, cvs)
            app.open_to = job["reach"]["label"]
            app.extras.filter(resume=job["resume"], status=Status.SKIPPED).update(
                note="The first application for this job now uses this CV, so this one isn't needed."
            )
        app.resume = job["resume"]
        app.title = job["title"]
        app.company = job["company"]
        app.location = job["location"]
        app.rules_met = job["rules_met"]
        app.score = job["match_score"]
        app.reason = job["reason"]
        app.subject = job["subject"]
        app.body = job["body"]
        if app.parent_id:  # a further CV goes where the first did, never to an address of its own choosing
            named = app.recipient if app.auto_send else ""
        else:
            # Only an address Gemini names goes out by itself. On a pasted job the best guess is
            # filled in for review, because the only address may be a footer's privacy contact.
            named = emails.choose_recipient(app.candidates, job["application_email"], app.jd_text, strict=True)
            crawled = app.source == Source.CRAWL
            app.recipient = named or emails.choose_recipient(app.candidates, "", app.jd_text, strict=crawled)
        app.status, app.note = _decide(app, job, profile)
        held = job["reach"]["hold"] if app.status in (Status.READY, Status.FORM_QUEUE, Status.MANUAL) else ""
        if job.get("review") or held:
            app.auto_send = False
            app.status = Status.MANUAL if app.status == Status.FORM_QUEUE else app.status
            app.note = " ".join(part for part in (held, app.note, job.get("review", "")) if part)
        elif app.status == Status.READY and app.auto_send and app.recipient and not named:
            app.note = UNCONFIRMED_ADDRESS
        elif app.parent_id and app.status == Status.READY and not app.auto_send and not app.note:
            if app.parent and app.parent.status == Status.SENT:
                app.note = "Drafted for you to check. Click Send when you're happy with it."
            else:
                app.note = "The first application for this job waits for you, so this one does too. Check it, then click Send."
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
    if not app.parent_id and app.status not in (Status.SKIPPED, Status.FAILED):
        # the others go by themselves only if this one did: to an address Gemini confirmed, or through the site's form
        went = auto_send and not job["reach"]["hold"] and (bool(named) or app.status == Status.FORM_QUEUE)
        try:
            _apply_with_other_cvs(app, job, profile, went)
        except Exception:  # a CV removed meanwhile, say: this one is done, and a crawl goes on
            log.exception("Applying to job %s with your other CVs failed", app_id)


def _addresses(cv):
    """The addresses an application with this CV goes out under: its own, and the Gmail account it sends from."""
    return {emails.mailbox(address) for address in (cv.email, sender(cv)) if address}


def _apply_with_other_cvs(app, job, profile, auto_send):
    """A remote job the posting opens to people anywhere also gets the best CV of each of your other countries, as a
    separate application, if it scores at least your minimum. Never twice from one address or with one CV.
    auto_send: whether the first went by itself, even if its draft was then held back for a quality problem of its
    own. Each goes to the first one's recipient, or through the same form."""
    reach = job["reach"]
    if not (profile.cv_per_country and reach["several"]):
        return
    group = [app, *app.extras.exclude(status=Status.FAILED).select_related("resume")]  # failed ones may try again
    held = [other.resume for other in group if other.resume]
    going = [other.resume for other in group if other.resume and other.status != Status.SKIPPED]  # their addresses
    countries_held = {countries.key(cv.country) for cv in held if cv.country}
    existing = set(Resume.objects.filter(pk__in=[cv.pk for cv in reach["allowed"]]).values_list("pk", flat=True))
    by_country = {}
    for cv in reach["allowed"]:
        place, score = countries.key(cv.country), reach["scores"][cv.pk]
        if place and place not in countries_held and cv not in held and cv.pk in existing and score >= min_score():
            by_country.setdefault(place, []).append(cv)
    for options in sorted(by_country.values(), key=lambda options: -max(reach["scores"][cv.pk] for cv in options)):
        options.sort(key=lambda cv: -reach["scores"][cv.pk])
        cv = next((cv for cv in options if not any(_addresses(other) & _addresses(cv) for other in going)), None)
        if cv is None:  # two applications from one address look like spam, so the best is only noted
            _note_held_back(app, options[0], next(other for other in going if _addresses(other) & _addresses(options[0])),
                            reach["scores"][options[0].pk])
            continue
        extra, is_new = _claim(
            source_hash(f"{app.source_hash} cv {cv.pk}"),
            auto_send=auto_send,
            **_same_job(app, cv),
        )
        if is_new:
            process(extra.pk)
            extra.refresh_from_db(fields=["status"])
        if extra.status not in (Status.SKIPPED, Status.FAILED):
            going.append(cv)


def _same_job(app, cv):
    return {
        "source": app.source, "crawl": app.crawl, "job_url": app.job_url, "jd_text": app.jd_text,
        "candidates": app.candidates, "recipient": app.recipient, "parent": app, "resume": cv, "open_to": app.open_to,
    }


def _note_held_back(app, cv, holder, score):
    if cv.email and emails.mailbox(cv.email) == emails.mailbox(holder.email):
        clash = f"it has the same email address as {holder.name}. Give it its own email in Setup"
    else:
        clash = (
            f"it would send from the same Gmail account as {holder.name}. Connect a Gmail account with its own address, "
            "or choose another one for it in Setup"
        )
    Application.objects.get_or_create(
        source_hash=source_hash(f"{app.source_hash} cv {cv.pk}"),
        defaults={
            **_same_job(app, cv), "auto_send": False, "status": Status.SKIPPED, "title": app.title,
            "company": app.company, "location": app.location, "score": score,
            "note": f"{cv.name} fits this job too (match {score}), but {clash}, or click Retry to draft it anyway.",
        },
    )


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
        own = {emails.mailbox(address) for address in own_addresses()}
        app.jd_text = page.text[:STORED_JD_CHARS]
        app.candidates = [address for address in page.emails if emails.mailbox(address) not in own]
        app.save()
    process(app_id)


# ---------------------------------------------------------------- Sending


def _cv_gone(app):
    if app.parent_id:  # it was for that CV only
        return "The CV for this application was removed, so it can't be sent. Your other CVs have their own."
    return "The CV chosen for this job was removed. Click Rewrite with Gemini to pick another."


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
        return _cv_gone(app)
    if not cv.full_name:
        return f"Add your name to {cv.name} in Setup first, so the email can be signed."
    if not mailer.is_configured():
        return "Connect Gmail first."
    if not sender(cv):
        mine = f"Connect {cv.email}, or choose" if cv.email else "Choose"
        return f"{mine} which Gmail account sends {cv.name}'s emails in Setup."
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


SAME_JOB_ALREADY = (
    "An application for this job already went out with this CV or from this email address, so this one waits. "
    "Click Send if you still want it to go."
)


def _went_with(app, cv, account=""):
    """Another application for this job that went, or is going, with this CV, its email address or this Gmail
    account. Checked as each one goes, because accounts and CVs can change after the job was read."""
    group = app.parent_id or app.pk
    mine = {emails.mailbox(address) for address in (cv.email, account) if address}
    others = (
        Application.objects.filter(Q(pk=group) | Q(parent_id=group))
        .exclude(pk=app.pk)
        .filter(status__in=[Status.SENT, Status.SENDING, Status.FILLING])
        .select_related("resume")
    )
    for other in others:
        theirs = {emails.mailbox(address) for address in (other.sender, other.resume.email if other.resume else "") if address}
        if other.resume_id == cv.pk or mine & theirs:
            return other
    return None


def deliver(app_id, by_hand=False):
    """Sends a row already claimed with claim_for_sending(). by_hand: you pressed Send, so it goes even if another
    application for the job went out under the same address."""
    with _send_lock:
        app = Application.objects.get(pk=app_id)
        try:
            cv = app.resume
            problem = _send_problem(app, cv)
            if not problem and not by_hand and _went_with(app, cv, sender(cv)):
                problem = SAME_JOB_ALREADY
            if problem:
                app.status, app.note = Status.READY, problem
                app.save()
                return
            app.sender = sender(cv)  # on record before it goes, so an interrupted send says where to look
            Application.objects.filter(pk=app.pk).update(sender=app.sender)
            mailer.send(
                to=app.recipient,
                subject=app.subject or "Job application",
                body=app.body,
                from_name=cv.full_name,
                attachment_name=attachment_name(cv),
                attachment=bytes(cv.data),
                mimetype=resume.mimetype(cv.name),
                sender=app.sender,
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


# ---------------------------------------------------------------- Job descriptions


TIDY_AGAIN_AFTER = 600  # seconds before another try at a job Gemini couldn't tidy
_tidy_lock = threading.Lock()
_tidying, _untidy = set(), {}  # jobs Gemini is tidying now, and when it last couldn't


def tidy_later(app):
    """Starts Gemini tidying a job's description the first time you open it, for a crawled page or a paste that
    brought a whole page with it. Pasted adverts are laid out as they are, without Gemini. True while it runs."""
    group = app.parent_id or app.pk
    messy = app.source == Source.CRAWL or jobtext.noisy(app.jd_text)
    if app.description or not app.jd_text.strip() or not messy or not llm.api_key():
        return False
    with _tidy_lock:
        if group in _tidying:
            return True
        if time.monotonic() - _untidy.get(group, -TIDY_AGAIN_AFTER) < TIDY_AGAIN_AFTER:
            return False
        _tidying.add(group)
    spawn(tidy_description, group)
    return True


def tidy_description(group):
    """One Gemini call, kept only if it holds to the posting's own words. The job's other applications share it.
    Nobody waits on it, since the page shows the job's own text meanwhile, so it can wait out an overloaded Gemini."""
    try:
        text = Application.objects.values_list("jd_text", flat=True).get(pk=group)[:JD_CHARS]
        raw = llm.generate_json(prompts.tidy_prompt(text), prompts.TIDY_SCHEMA, system=prompts.TIDY_SYSTEM, patient=True)
        tidied = jobtext.markup(raw.get("blocks"))
        if tidied and jobtext.faithful(tidied, text):
            Application.objects.filter(Q(pk=group) | Q(parent_id=group), description="").update(description=tidied)
        else:
            log.warning("Gemini's tidied description for job %s strayed from the posting, so it wasn't used", group)
            _untidy[group] = time.monotonic()
    except Exception as exc:  # the job's own text still shows, laid out
        log.warning("Tidying the description for job %s failed: %s", group, exc)
        _untidy[group] = time.monotonic()
    finally:
        with _tidy_lock:
            _tidying.discard(group)


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
    """Everything the form is filled in with comes from the CV this job got: name, email, phone and links."""
    message = app.body
    stem = attachment_name(cv).split("_CV")[0]
    return formfill.Job(
        url=app.job_url,
        full_name=cv.full_name,
        email=cv.email or sender(cv),
        phone=cv.phone,
        linkedin=cv.linkedin,
        github=resume.github_profile(cv.links),
        message=message,
        resume_name=attachment_name(cv),
        resume_bytes=bytes(cv.data),
        cover_name=f"{stem}_Cover_Letter.docx",
        cover_bytes=resume.letter_as_docx(message),
        allow_consent=profile.allow_consent,
        submit=submit,
        stay_open=stay_open,
        ask=lambda fields: _ask_gemini(app, cv, fields),
    )


def _cv_links(cv):
    linkedin = cv.linkedin if not cv.linkedin or cv.linkedin.lower().startswith("http") else f"https://{cv.linkedin}"
    return [link for link in (linkedin, *cv.links.split()) if link]


def _ask_gemini(app, cv, fields):
    try:
        raw = llm.generate_json(
            prompts.form_prompt(
                cv.full_name, cv.text[:CV_CHARS], _cv_links(cv), facts(cv), spelling(cv), app.jd_text[:JD_CHARS], fields
            ),
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
            outcome = formfill.Outcome(formfill.NEEDS_YOU, _cv_gone(app))
        elif not cv.full_name:
            outcome = formfill.Outcome(formfill.NEEDS_YOU, f"Add your name to {cv.name} in Setup first, then press Fill the form.")
        elif app.auto_send and _went_with(app, cv, cv.email or sender(cv)):
            outcome = formfill.Outcome(formfill.NEEDS_YOU, SAME_JOB_ALREADY.replace("Click Send", "Press Fill the form"))
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

    own = {emails.mailbox(address) for address in own_addresses()}
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
        app.candidates = [address for address in page.emails if emails.mailbox(address) not in own]
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
