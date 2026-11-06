"""Settings for a single-user tool that only ever runs on 127.0.0.1."""
import os
import re
import secrets
import zoneinfo
from pathlib import Path

from dotenv import load_dotenv


def _machine_time_zone():
    """Times on the dashboard, and what counts as "today", follow this computer's clock."""
    match = re.search(r"zoneinfo[^/]*/(.+)$", os.path.realpath("/etc/localtime"))
    try:
        zoneinfo.ZoneInfo(match.group(1))
    except Exception:
        return "Europe/London"
    return match.group(1)


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
load_dotenv(ENV_FILE)

# Playwright's browser thread looks "async" to Django, which would refuse database calls from it.
# Nothing in this app is really async, so those calls are safe. This is Django's documented switch.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

# Nothing long-lived is signed, so a fresh key per run means no secret to manage.
SECRET_KEY = secrets.token_urlsafe(50)
DEBUG = False
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

INSTALLED_APPS = ["django.contrib.messages", "core"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",  # enforces ALLOWED_HOSTS, which stops DNS rebinding
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
ROOT_URLCONF = "jobfinder.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": ["django.contrib.messages.context_processors.messages"],
        },
    }
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "db.sqlite3",
        "OPTIONS": {"timeout": 30},
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
MESSAGE_STORAGE = "django.contrib.messages.storage.cookie.CookieStorage"
CSRF_COOKIE_NAME = "jobfinder_csrf"
USE_TZ = True
TIME_ZONE = _machine_time_zone()
LANGUAGE_CODE = "en-gb"
