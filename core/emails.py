"""Finds email addresses in text.

Addresses always come from the text itself, never from the LLM, so a web page
cannot talk the app into mailing an address that isn't printed on it.
"""
import re

from django.core.exceptions import ValidationError
from django.core.validators import validate_email

_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"))
_DOMAIN = r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,24}"
# Local parts are capped at the real limit of 64 characters, which also keeps scanning a hostile page fast.
EMAIL_RE = re.compile(rf"(?<![\w.+%-])[A-Za-z0-9][A-Za-z0-9._%+'-]{{0,63}}@{_DOMAIN}(?!\w)")

# Text stuck to the end of an address: jobs@acme.comApply, jobs@acme.com.Thanks
_GLUED = re.compile(r"(@[^@\s]*\.[a-z]{2,})(?:\.(?!(?:Com|Org|Net|Uk|Io|Co)$)[A-Z][a-z]*|[A-Z][^.]*)$")

# Written-out forms: jobs [at] acme [dot] com, jobs(at)acme.com, jobs at acme dot com
# "x @ y" with spaces is deliberately not one: "Head of Talent @ Cal.com" isn't an address.
_BRACKET_AT = re.compile(r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|<\s*at\s*>)\s*", re.I)
_BRACKET_DOT = re.compile(r"\s*(?:\[\s*dot\s*\]|\(\s*dot\s*\)|\{\s*dot\s*\}|<\s*dot\s*>)\s*", re.I)
_WORD_FORM = re.compile(
    r"\b([A-Za-z0-9][\w.+-]{0,63})\s+at\s+((?:[A-Za-z0-9-]+\s+dot\s+)+[A-Za-z]{2,24})\b(?!\.\w)", re.I
)
_WORD_DOT = re.compile(r"\s+dot\s+", re.I)
_NOT_A_MAILBOX = {
    "apply", "available", "based", "found", "here", "listed", "located", "look", "me",
    "online", "page", "posted", "site", "us", "visit", "website", "work", "working",
}

_FILE_TLDS = {
    "png", "jpg", "jpeg", "gif", "svg", "webp", "bmp", "ico", "css", "js", "json",
    "pdf", "doc", "docx", "zip", "mp4", "woff", "woff2", "ttf", "eot",
}
_NO_REPLY = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|dont[-_.]?reply|mailer[-_.]?daemon"
    r"|bounces?|notifications?|alerts?|unsubscribe)",
    re.I,
)
_NOT_RECRUITING = {
    "abuse", "accounts", "billing", "complaints", "compliance", "customercare",
    "customerservice", "customersupport", "dataprotection", "dpo", "feedback", "finance",
    "gdpr", "help", "helpdesk", "hostmaster", "invoices", "legal", "marketing", "media",
    "postmaster", "pr", "press", "privacy", "sales", "security", "support", "webmaster",
}
_FAKE_DOMAINS = {
    "company.com", "domain.com", "email.com", "example.com", "example.net", "example.org",
    "sentry.io", "test.com", "wixpress.com", "yourcompany.com", "yourdomain.com",
}
_RECRUITING_LOCAL = re.compile(
    r"career|job|recruit|talent|hiring|hr\b|people|apply|applicant|application|vacanc|cv|resume", re.I
)
_GENERIC_LOCAL = re.compile(r"info|hello|contact|office|admin|enquir|team|mail", re.I)
_APPLY_CONTEXT = re.compile(r"appl|send|email|e-mail|cv|resume|contact", re.I)


def _normalise(address):
    return _GLUED.sub(r"\1", (address or "").strip()).lower()


def all_addresses(text):
    """Every address-shaped string in the text, in order of appearance."""
    text = (text or "").translate(_INVISIBLE).replace("＠", "@")
    text = re.sub(r"\s+", " ", text)  # also keeps the patterns below fast on long blank runs
    found = EMAIL_RE.findall(text)
    revealed = _WORD_DOT.sub(".", _BRACKET_DOT.sub(".", _BRACKET_AT.sub("@", text)))
    found += EMAIL_RE.findall(revealed)
    for local, domain in _WORD_FORM.findall(text):
        # With no brackets, "now at bluefin dot io" looks the same as a real address,
        # so only trust an obvious mailbox or a dotted name.
        if local.lower() not in _NOT_A_MAILBOX and (_RECRUITING_LOCAL.search(local) or "." in local):
            found.append(f"{local}@{_WORD_DOT.sub('.', domain)}")
    return list(dict.fromkeys(_normalise(address) for address in found))


def is_usable(address):
    """False for no-reply boxes, junk matches and mailboxes that don't take CVs."""
    try:
        validate_email(address)
    except ValidationError:
        return False
    local, domain = address.rsplit("@", 1)
    if domain.rsplit(".", 1)[-1] in _FILE_TLDS or _NO_REPLY.match(local):
        return False
    if any(domain == fake or domain.endswith("." + fake) for fake in _FAKE_DOMAINS):
        return False
    if re.fullmatch(r"[0-9a-f]{16,}", local):  # tracking ids, e.g. Sentry DSNs
        return False
    return re.sub(r"[._-]", "", local) not in _NOT_RECRUITING


def mailbox(address):
    """One spelling per mailbox, for telling whether two addresses reach the same inbox. Gmail ignores dots and
    anything after a + in the name, and googlemail.com is gmail.com."""
    address = (address or "").strip().lower()
    local, _, domain = address.rpartition("@")
    if not local:
        return address
    local = local.split("+", 1)[0]
    if domain in ("gmail.com", "googlemail.com"):
        return f"{local.replace('.', '')}@gmail.com"
    return f"{local}@{domain}"


def find_emails(text, exclude=()):
    """Addresses worth applying to, leaving out the applicant's own: one address, or several."""
    own = {mailbox(address) for address in ([exclude] if isinstance(exclude, str) else exclude or ()) if address}
    return [a for a in all_addresses(text) if mailbox(a) not in own and is_usable(a)]


def best_guess(candidates, text):
    """The likeliest application address, for when the LLM doesn't name one."""
    lowered = (text or "").lower()

    def score(address):
        local = address.split("@")[0]
        value = 3 if _RECRUITING_LOCAL.search(local) else 0
        value -= 1 if _GENERIC_LOCAL.search(local) else 0
        index = lowered.find(address)
        if index >= 0 and _APPLY_CONTEXT.search(lowered[max(0, index - 120):index]):
            value += 2
        return value

    return max(candidates, key=score)  # max keeps the first of equal scores


def choose_recipient(candidates, llm_choice, text, strict):
    """Pick from what's printed in the text; whatever the LLM says can't add to it.

    strict: only accept an address the LLM confirmed is for applications. Used
    for crawled pages, where a footer's contact@ address isn't an invitation.
    """
    choice = _normalise(llm_choice)
    if choice in candidates:
        return choice
    if strict or not candidates:
        return ""
    return best_guess(candidates, text)
