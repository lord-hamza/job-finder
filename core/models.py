from django.db import models


class Profile(models.Model):
    """You: the details, facts and job rules that go with every CV. There's only one."""

    DEFAULT_ONSITE_PLACES = "Pakistan, Lahore, Islamabad, Karachi, Rawalpindi"
    DEFAULT_REMOTE_COUNTRIES = "United Kingdom, United States"

    full_name = models.CharField(max_length=120, blank=True)
    phone = models.CharField(max_length=40, blank=True)
    linkedin = models.CharField(max_length=200, blank=True)
    facts = models.TextField(blank=True)  # what forms ask that a CV doesn't say
    allow_consent = models.BooleanField(default=False)  # may tick required privacy boxes
    onsite_places = models.CharField(max_length=300, blank=True, default=DEFAULT_ONSITE_PLACES)  # any work setup here
    remote_countries = models.CharField(max_length=300, blank=True, default=DEFAULT_REMOTE_COUNTRIES)  # blank: anywhere
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def load(cls):
        return cls.objects.order_by("pk").first() or cls.objects.create()

    @property
    def rule_summary(self):
        remote = f"fully remote jobs from {self.remote_countries or 'anywhere'}"
        return f"any work setup in {self.onsite_places}, and {remote}" if self.onsite_places else f"only {remote}"


class Resume(models.Model):
    """One CV. Gemini picks whichever suits each job best."""

    MAX_COUNT = 10

    name = models.CharField(max_length=200)
    data = models.BinaryField()
    text = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class CrawlRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"

    url = models.URLField(max_length=2000)
    auto_send = models.BooleanField(default=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.RUNNING)
    total = models.PositiveIntegerField(default=0)
    done = models.PositiveIntegerField(default=0)
    note = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class Application(models.Model):
    class Source(models.TextChoices):
        PASTE = "paste", "Pasted"
        CRAWL = "crawl", "Crawled"

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        PROCESSING = "processing", "Tailoring"
        READY = "ready", "Ready to send"
        SENDING = "sending", "Sending"
        FORM_QUEUE = "form_queue", "Waiting for form"
        FILLING = "filling", "Filling form"
        SENT = "sent", "Sent"
        NEEDS_YOU = "needs_you", "Needs you"
        MANUAL = "manual", "Apply manually"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"

    class Channel(models.TextChoices):
        EMAIL = "email", "Email"
        FORM = "form", "Website form"

    # One row per job: the hash of the pasted text or the job's URL.
    source_hash = models.CharField(max_length=64, unique=True)
    source = models.CharField(max_length=5, choices=Source.choices)
    crawl = models.ForeignKey(
        CrawlRun, null=True, blank=True, on_delete=models.SET_NULL, related_name="applications"
    )
    auto_send = models.BooleanField(default=True)
    resume = models.ForeignKey(
        Resume, null=True, blank=True, on_delete=models.SET_NULL, related_name="applications"
    )  # the CV chosen for it
    job_url = models.URLField(max_length=2000, blank=True)
    jd_text = models.TextField(blank=True)
    candidates = models.JSONField(default=list, blank=True)  # addresses printed in the JD

    title = models.CharField(max_length=300, blank=True)
    company = models.CharField(max_length=200, blank=True)
    location = models.CharField(max_length=200, blank=True)
    rules_met = models.CharField(max_length=10, blank=True)  # does the job meet your job rules?
    score = models.IntegerField(null=True, blank=True)
    reason = models.TextField(blank=True)

    recipient = models.EmailField(max_length=254, blank=True)
    subject = models.CharField(max_length=300, blank=True)
    body = models.TextField(blank=True)

    status = models.CharField(max_length=10, choices=Status.choices, default=Status.QUEUED)
    note = models.TextField(blank=True)  # why it is in this status
    channel = models.CharField(max_length=5, choices=Channel.choices, default=Channel.EMAIL)
    form_answers = models.JSONField(default=list, blank=True)  # what went into the website form
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    @property
    def in_flight(self):
        return self.status in IN_FLIGHT

    @property
    def can_send(self):
        return bool(self.body) and not self.in_flight and self.status != self.Status.SENT

    @property
    def can_retry(self):
        return not self.in_flight and self.status != self.Status.SENT

    @property
    def can_fill_form(self):
        return bool(self.job_url) and bool(self.body) and self.can_retry

    @property
    def can_mark_applied(self):
        return self.status in (self.Status.NEEDS_YOU, self.Status.MANUAL)


IN_FLIGHT = (
    Application.Status.QUEUED,
    Application.Status.PROCESSING,
    Application.Status.SENDING,
    Application.Status.FORM_QUEUE,
    Application.Status.FILLING,
)
