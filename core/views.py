import os

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_POST
from dotenv import set_key

from . import countries, emails, llm, mailer, pipeline, prompts, resume
from .models import IN_FLIGHT, Application, CrawlRun, Profile, Resume

MIN_JD_CHARS = 100
Status = Application.Status


def _save_env(**values):
    """.env stays the single home for secrets; the dashboard is just a way to fill it in."""
    for key, value in values.items():
        set_key(str(settings.ENV_FILE), key, value)
        os.environ[key] = value
    os.chmod(settings.ENV_FILE, 0o600)


def _render_home(request, **context):
    pipeline.recover_interrupted()
    profile = Profile.load()
    resumes = list(Resume.objects.defer("data"))
    accounts = mailer.accounts()
    covered = [countries.key(place) for place in pipeline.split_places(f"{profile.onsite_places},{profile.remote_countries}")]
    for cv in resumes:
        cv.facts_text = pipeline.facts(cv)
        cv.chosen_account = mailer.account(cv.send_from)
        cv.auto_account = mailer.account(cv.email) or (accounts[0] if len(accounts) == 1 else "")
        cv.no_sender = bool(accounts) and not pipeline.sender(cv)
        cv.outside_rules = bool(cv.country and profile.remote_countries and countries.key(cv.country) not in covered)
        cv.shares_email = any(
            other.pk != cv.pk and cv.email and emails.mailbox(other.email) == emails.mailbox(cv.email) for other in resumes
        )
        mine = pipeline.sender(cv)
        cv.shares_account = not cv.shares_email and any(
            other.pk != cv.pk and mine and pipeline.sender(other) == mine for other in resumes
        )
    missing = pipeline.missing_setup()
    attention = [f"choose a Gmail account for {cv.name}" for cv in resumes if cv.no_sender]
    return render(
        request,
        "core/home.html",
        {
            "profile": profile,
            "resumes": resumes,
            "missing": missing,
            "attention": attention,
            "setup_open": bool(missing or attention or context.get("add_gmail")) or any(not cv.facts for cv in resumes),
            "accounts": accounts,
            "max_accounts": mailer.MAX_ACCOUNTS,
            "country_names": countries.NAMES,
            "gmail_connected": bool(accounts),
            "gemini_connected": bool(llm.api_key()),
            "min_score": pipeline.min_score(),
            **context,
        },
    )


@require_GET
def home(request):
    return _render_home(request, add_gmail=request.GET.get("add") == "gmail")


def _filters():
    """The activity tabs: (key, label, rows). Plain links, so the list refreshes itself on the tab you chose."""
    midnight = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    everything = Application.objects.all()
    return [
        ("all", "All", everything),
        ("today", "sent today", everything.filter(status=Status.SENT, sent_at__gte=midnight)),
        ("sent", "sent in total", everything.filter(status=Status.SENT)),
        ("ready", "ready to send", everything.filter(status=Status.READY)),
        ("you", "need you", everything.filter(status__in=[Status.MANUAL, Status.NEEDS_YOU])),
        ("skipped", "skipped", everything.filter(status=Status.SKIPPED)),
        ("failed", "failed", everything.filter(status=Status.FAILED)),
    ]


@require_GET
@xframe_options_sameorigin
def activity(request):
    """Shown in an iframe so it can refresh itself without touching the forms around it."""
    pipeline.recover_interrupted()
    pipeline.ensure_form_worker()
    filters = _filters()
    show = request.GET.get("show", "all")
    if show not in {key for key, _, _ in filters}:
        show = "all"
    rows = next(rows for key, _, rows in filters if key == show)
    applications = list(rows.select_related("resume")[:200])
    runs = list(CrawlRun.objects.all()[:5]) if show == "all" else []
    working = Application.objects.filter(status__in=IN_FLIGHT).exists()
    busy = working or CrawlRun.objects.filter(status=CrawlRun.Status.RUNNING).exists()
    return render(
        request,
        "core/activity.html",
        {
            "applications": applications,
            "runs": runs,
            "busy": busy,
            "show": show,
            "tabs": [(key, label, rows.count()) for key, label, rows in filters],
            "daily_limit": pipeline.daily_limit(),
        },
    )


@require_POST
def delete_resume(request, pk):
    Resume.objects.filter(pk=pk).delete()
    messages.success(request, "CV removed.")
    return redirect("home")


@require_POST
def save_setup(request):
    """Everything in Setup at once: each CV's details, your job rules and the consent box."""
    profile = Profile.load()
    profile.onsite_places = _places(request.POST.get("onsite_places", ""))
    profile.remote_countries = _places(request.POST.get("remote_countries", ""))
    profile.cv_per_country = request.POST.get("cv_per_country") == "on"
    profile.allow_consent = request.POST.get("allow_consent") == "on"
    profile.save()
    resigned = 0
    for cv in Resume.objects.defer("data"):
        prefix = f"cv-{cv.pk}-"
        if f"{prefix}full_name" not in request.POST:
            continue  # uploaded after this page was opened
        fields = {key[len(prefix):]: value for key, value in request.POST.items() if key.startswith(prefix)}
        problems, count = _save_cv(cv, fields)
        resigned += count
        if problems:
            messages.error(request, f"{cv.name}: {'; and '.join(problems)}. The rest is saved.")
    emails = f" {resigned} unsent email{'s' if resigned > 1 else ''} now signed with the new details." if resigned else ""
    messages.success(request, f"Setup saved.{emails}")
    _upload(request, request.FILES.getlist("resume"))  # after the details: a new file's own details then win
    return redirect("home")


def _upload(request, uploads):
    """CVs chosen in Setup. A file with the same name replaces that CV, keeping its facts and anything you typed."""
    saved, replaced = 0, []
    for upload in uploads:
        try:
            if upload.size > resume.MAX_BYTES:
                raise resume.ResumeError("That file is over 10 MB, which is too big to attach to an application.")
            cv = pipeline.save_resume(upload.name, upload.read())
        except resume.ResumeError as exc:
            messages.error(request, f"{upload.name}: {exc}")
        else:
            saved += 1
            if cv.replaced:
                replaced.append(cv.name)
    if saved:
        cvs = "1 CV" if saved == 1 else f"{saved} CVs"
        messages.success(
            request,
            f"{cvs} uploaded. Check the name, contact details and country read from each below, and fill in its About you.",
        )
    if replaced:
        messages.info(
            request,
            f"Replaced {', '.join(replaced)}, keeping the facts you'd saved and anything you typed. "
            "If it's a different CV, check them.",
        )


def _places(text):
    return ", ".join(pipeline.split_places(text))[:300]


def _one_line(value, size):
    return " ".join((value or "").split())[:size]


def _save_cv(cv, fields):
    """Who one CV says you are, and what goes out with it. A value that can't be used keeps the saved one, and
    everything else is still saved. Returns (problems, how many unsent emails were signed again)."""
    old_name, old_phone, old_linkedin = cv.full_name, cv.phone, cv.linkedin
    problems = []
    name = _one_line(fields.get("full_name"), 120)
    if name:
        cv.full_name = name
    else:
        problems.append("the name is needed to sign its emails, so the old one stays")
    address = _one_line(fields.get("email"), 254).lower()
    try:
        if address:
            validate_email(address)
        cv.email = address
    except ValidationError:
        problems.append(f"{address} isn't a valid email address, so the old one stays")
    if "send_from" in fields:  # only offered while an account is connected
        send_from = fields["send_from"].strip()
        if send_from and not mailer.account(send_from):
            problems.append(f"{send_from} isn't a connected Gmail account")
        else:
            cv.send_from = mailer.account(send_from)
    text = (fields.get("facts") or "").replace("\r\n", "\n").strip()
    cv.phone = _one_line(fields.get("phone"), 40)
    cv.linkedin = _one_line(fields.get("linkedin"), 200)
    cv.links = "\n".join(resume.clean_links(fields.get("links", "")))
    cv.country = countries.canonical(_one_line(fields.get("country"), 60))[:60]
    cv.facts = "" if text == prompts.FACTS_TEMPLATE else text[:5000]
    cv.save(update_fields=["full_name", "email", "phone", "linkedin", "links", "country", "send_from", "facts"])
    return problems, pipeline.resign_drafts(cv, old_name, old_phone, old_linkedin)


@require_POST
def connect_gmail(request):
    address = request.POST.get("address", "").strip().lower()
    password = "".join(request.POST.get("password", "").split())
    try:
        validate_email(address)
    except ValidationError:
        messages.error(request, "Enter a valid Gmail address.")
        return _back_to_gmail()
    if not password:
        messages.error(request, "Enter your Gmail App Password.")
        return _back_to_gmail()
    slot = mailer.slot_for(address)
    if slot is None:
        messages.error(request, f"You can connect up to {mailer.MAX_ACCOUNTS} Gmail accounts. Remove one first.")
        return _back_to_gmail()
    try:
        mailer.verify(address, password)
    except mailer.MailError as exc:
        messages.error(request, str(exc))
        return _back_to_gmail()
    else:
        user_key, secret_key = mailer.env_keys(slot)
        _save_env(**{user_key: address, secret_key: password})
        messages.success(request, f"Gmail connected as {address}.{_waiting_for_an_account()}")
    return redirect("home")


def _back_to_gmail():
    """Back to the open Add a Gmail account form, so a mistake is quick to fix."""
    return redirect(f"{reverse('home')}?add=gmail#add-gmail")


def _waiting_for_an_account():
    """With more than one account, a CV whose own address isn't connected needs one chosen for it."""
    waiting = [cv.name for cv in Resume.objects.defer("data") if mailer.is_configured() and not pipeline.sender(cv)]
    return f" {', '.join(waiting)} now need an account to send from: choose one below." if waiting else ""


@require_POST
def disconnect_gmail(request):
    address = request.POST.get("address", "").strip()
    slots = mailer.slots_of(address)
    for slot in slots:
        user_key, secret_key = mailer.env_keys(slot)
        _save_env(**{user_key: "", secret_key: ""})
    if slots:
        messages.success(request, f"Removed {address}.{_waiting_for_an_account()}")
    return redirect("home")


@require_POST
def save_gemini(request):
    key = request.POST.get("key", "").strip()
    if not key or not key.isascii() or any(c.isspace() for c in key):
        messages.error(request, "Enter a valid Gemini API key.")
        return redirect("home")
    try:
        llm.verify_key(key)
    except llm.LLMError as exc:
        messages.error(request, str(exc))
    else:
        _save_env(GEMINI_API_KEY=key)
        messages.success(request, "Gemini connected.")
    return redirect("home")


@require_POST
def apply_jd(request):
    text = request.POST.get("jd", "").strip()
    auto_send = request.POST.get("action") != "draft"

    def refuse(message):
        messages.error(request, message)
        return _render_home(request, jd_text=text)

    missing = pipeline.missing_setup(need_gmail=auto_send)
    if missing:
        return refuse(f"First, {', '.join(missing)}.")
    if len(text) < MIN_JD_CHARS:
        return refuse("Paste the whole job description, including the email address to apply to.")
    if auto_send and not emails.find_emails(text, exclude=pipeline.own_addresses()):
        ignored = emails.all_addresses(text)
        if ignored:
            return refuse(
                f"Only found {', '.join(ignored[:3])}, which won't take applications. "
                "Use Save as draft to type the right address yourself."
            )
        return refuse("No email address in that text. Add one, or use Save as draft to type it yourself.")

    app, is_new = pipeline.submit_jd(text, auto_send)
    if is_new:
        pipeline.spawn(pipeline.process, app.pk)
        messages.success(request, "On it. Progress shows in the activity list below.")
        return redirect("home")
    messages.info(request, f"You've already added this job: {app.get_status_display()}.")
    return redirect("detail", pk=app.pk)


@require_POST
def start_crawl(request):
    url = request.POST.get("url", "").strip()
    auto_send = request.POST.get("auto_send") == "on"

    def refuse(message):
        messages.error(request, message)
        return _render_home(request, crawl_url=url, crawl_auto_send=auto_send)

    missing = pipeline.missing_setup(need_gmail=auto_send)
    if missing:
        return refuse(f"First, {', '.join(missing)}.")
    if url and "://" not in url:
        url = f"https://{url}"
    try:
        URLValidator(schemes=["http", "https"])(url)
    except ValidationError:
        return refuse("Enter a valid web link, like https://example.com/jobs")
    if len(url) > 2000:
        return refuse("That link is too long.")
    run = CrawlRun.objects.create(url=url, auto_send=auto_send)
    pipeline.spawn(pipeline.run_crawl, run.pk)
    messages.success(request, "Crawl started. Progress shows in the activity list below.")
    return redirect("home")


@require_GET
def detail(request, pk):
    pipeline.recover_interrupted()
    app = get_object_or_404(Application.objects.select_related("resume", "parent__resume"), pk=pk)
    group = app.parent or app
    related = [
        other for other in [group, *group.extras.select_related("resume").order_by("pk")] if other.pk != app.pk
    ]
    return render(
        request,
        "core/detail.html",
        {
            "app": app,
            "related": related,
            "attachment": pipeline.attachment_name(app.resume) if app.resume else "",
            "sends_from": pipeline.sender(app.resume) if app.resume else "",
            "was_sending": app.note == pipeline.INTERRUPTED_SEND and app.sender,
        },
    )


@require_POST
def send(request, pk):
    app = get_object_or_404(Application, pk=pk)
    if not app.can_send:
        messages.error(request, "This application can't be sent right now.")
        return redirect("detail", pk=pk)
    recipient = request.POST.get("recipient", "").strip().lower()
    subject = " ".join(request.POST.get("subject", "").split())[:300]
    body = request.POST.get("body", "").replace("\r\n", "\n").strip()
    try:
        validate_email(recipient)
    except ValidationError:
        messages.error(request, "Enter a valid recipient email address.")
        return redirect("detail", pk=pk)
    if not subject or not body:
        messages.error(request, "The subject and message can't be empty.")
        return redirect("detail", pk=pk)
    if pipeline.claim_for_sending(pk, recipient=recipient, subject=subject, body=body, note=""):
        pipeline.spawn(pipeline.deliver, pk, True)  # you pressed Send
    return redirect("detail", pk=pk)


@require_POST
def retry(request, pk):
    app = get_object_or_404(Application, pk=pk)
    # Rewriting an email already drafted goes back to the user to review, never straight out.
    auto_send = app.auto_send and not app.body
    claimed = (
        Application.objects.filter(pk=pk)
        .exclude(status__in=[Status.SENT, *IN_FLIGHT])
        .update(status=Status.QUEUED, note="", auto_send=auto_send, updated_at=timezone.now())
    )
    if claimed:
        pipeline.spawn(pipeline.retry, pk)
    else:
        messages.error(request, "This application can't be retried right now.")
    return redirect("detail", pk=pk)


@require_POST
def fill_form(request, pk):
    app = get_object_or_404(Application, pk=pk)
    if not app.can_fill_form or not pipeline.queue_form(pk):
        messages.error(request, "This form can't be filled in right now.")
    return redirect("detail", pk=pk)


@require_POST
def mark_applied(request, pk):
    get_object_or_404(Application, pk=pk)
    if not pipeline.mark_applied(pk):
        messages.error(request, "This application can't be marked as submitted right now.")
    return redirect("detail", pk=pk)
