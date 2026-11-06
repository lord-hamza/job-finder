import os

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_POST
from dotenv import set_key

from . import emails, llm, mailer, pipeline, prompts, resume
from .models import IN_FLIGHT, Application, CrawlRun, Profile

MIN_JD_CHARS = 100
Status = Application.Status


def _save_env(**values):
    """.env stays the single home for secrets; the dashboard is just a way to fill it in."""
    for key, value in values.items():
        set_key(str(settings.ENV_FILE), key, value)
        os.environ[key] = value
    os.chmod(settings.ENV_FILE, 0o600)


def _market(request):
    market = request.POST.get("market")
    return market if market in Profile.Market.values else Profile.Market.UK


def _ticked_markets(request):
    ticked = request.POST.getlist("cv")
    return [market for market in Profile.Market.values if market in ticked]


def _render_home(request, **context):
    pipeline.recover_interrupted()
    profiles = Profile.options()
    for profile in profiles:
        profile.facts_text = pipeline.facts(profile)
    missing = pipeline.missing_setup([profile.market for profile in profiles if profile.selected])
    return render(
        request,
        "core/home.html",
        {
            "profiles": profiles,
            "missing": missing,
            "setup_open": bool(missing) or any(not profile.has_resume or not profile.facts for profile in profiles),
            "gmail_address": mailer.address(),
            "gmail_connected": mailer.is_configured(),
            "gemini_connected": bool(llm.api_key()),
            "min_score": pipeline.min_score(),
            **context,
        },
    )


@require_GET
def home(request):
    return _render_home(request)


@require_GET
@xframe_options_sameorigin
def activity(request):
    """Shown in an iframe so it can refresh itself without touching the forms around it."""
    pipeline.recover_interrupted()
    pipeline.ensure_form_worker()
    applications = list(Application.objects.select_related("profile")[:200])
    runs = list(CrawlRun.objects.all()[:5])
    busy = any(app.in_flight for app in applications) or any(
        run.status == CrawlRun.Status.RUNNING for run in runs
    )
    return render(
        request,
        "core/activity.html",
        {
            "applications": applications,
            "runs": runs,
            "busy": busy,
            "sent_today": pipeline.sent_today(),
            "daily_limit": pipeline.daily_limit(),
            "total_sent": Application.objects.filter(status=Status.SENT).count(),
            "manual_count": Application.objects.filter(status__in=[Status.MANUAL, Status.NEEDS_YOU]).count(),
            "ready_count": Application.objects.filter(status=Status.READY).count(),
        },
    )


@require_POST
def upload_resume(request):
    upload = request.FILES.get("resume")
    if not upload:
        messages.error(request, "Choose a PDF or Word .docx file first.")
    elif upload.size > resume.MAX_BYTES:
        messages.error(request, "That file is over 10 MB, which is too big to attach to an application.")
    else:
        try:
            pipeline.save_resume(upload.name, upload.read(), _market(request))
        except resume.ResumeError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, "CV saved. Check that your details below are right.")
    return redirect("home")


@require_POST
def save_profile(request):
    name = " ".join(request.POST.get("full_name", "").split())
    if not name:
        messages.error(request, "Your name is needed to sign your emails.")
        return redirect("home")
    profile = Profile.load(_market(request))
    profile.full_name = name[:120]
    profile.phone = " ".join(request.POST.get("phone", "").split())[:40]
    profile.linkedin = " ".join(request.POST.get("linkedin", "").split())[:200]
    profile.save()
    messages.success(request, "Details saved.")
    return redirect("home")


@require_POST
def save_facts(request):
    text = request.POST.get("facts", "").replace("\r\n", "\n").strip()
    profile = Profile.load(_market(request))
    profile.facts = "" if text == prompts.FACTS_TEMPLATES[profile.market] else text[:5000]
    profile.allow_consent = request.POST.get("allow_consent") == "on"
    profile.save()
    messages.success(request, "Facts saved.")
    return redirect("home")


@require_POST
def connect_gmail(request):
    address = request.POST.get("address", "").strip().lower()
    password = "".join(request.POST.get("password", "").split())
    try:
        validate_email(address)
    except ValidationError:
        messages.error(request, "Enter a valid Gmail address.")
        return redirect("home")
    if not password:
        messages.error(request, "Enter your Gmail App Password.")
        return redirect("home")
    try:
        mailer.verify(address, password)
    except mailer.MailError as exc:
        messages.error(request, str(exc))
    else:
        _save_env(GMAIL_ADDRESS=address, GMAIL_APP_PASSWORD=password)
        messages.success(request, f"Gmail connected as {address}.")
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

    markets = _ticked_markets(request)
    if not markets:
        return refuse("Tick at least one CV to apply with.")
    pipeline.remember_selection(markets)
    missing = pipeline.missing_setup(markets, need_gmail=auto_send)
    if missing:
        return refuse(f"First, {', '.join(missing)}.")
    if len(text) < MIN_JD_CHARS:
        return refuse("Paste the whole job description, including the email address to apply to.")
    if auto_send and not emails.find_emails(text, exclude=mailer.address()):
        ignored = emails.all_addresses(text)
        if ignored:
            return refuse(
                f"Only found {', '.join(ignored[:3])}, which won't take applications. "
                "Use Save as draft to type the right address yourself."
            )
        return refuse("No email address in that text. Add one, or use Save as draft to type it yourself.")

    app, is_new = pipeline.submit_jd(text, auto_send, markets)
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

    markets = _ticked_markets(request)
    if not markets:
        return refuse("Tick at least one CV to apply with.")
    pipeline.remember_selection(markets)
    missing = pipeline.missing_setup(markets, need_gmail=auto_send)
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
    run = CrawlRun.objects.create(url=url, auto_send=auto_send, markets=markets)
    pipeline.spawn(pipeline.run_crawl, run.pk)
    messages.success(request, "Crawl started. Progress shows in the activity list below.")
    return redirect("home")


@require_GET
def detail(request, pk):
    pipeline.recover_interrupted()
    app = get_object_or_404(Application, pk=pk)
    profile = app.profile or Profile.load()
    return render(
        request,
        "core/detail.html",
        {"app": app, "attachment": pipeline.attachment_name(profile) if profile.has_resume else ""},
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
        pipeline.spawn(pipeline.deliver, pk)
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
