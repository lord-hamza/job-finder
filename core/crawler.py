"""Fetches web pages politely and turns them into text, links and email addresses."""
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError

from . import emails, formfill

USER_AGENT = "Mozilla/5.0 (compatible; JobFinder/1.0; personal job search tool)"
ROBOTS_AGENT = "JobFinder"
TIMEOUT = (10, 20)
DOWNLOAD_SECONDS = 30
MAX_BYTES = 3 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_LINKS = 500
MIN_TEXT = 200
PASTE_INSTEAD = "Paste the job description into the box instead."

_TRACKING = re.compile(r"^(utm_\w+|gclid|fbclid|msclkid|mc_cid|mc_eid|trk|trackingId|refId)$", re.I)
_PER_VISIT = re.compile(r"^(sid|source|filter)$", re.I)  # search context on a job's own page
_ID_AT_END = re.compile(r"/\d{5,}/?$")
_GENERIC_LABEL = re.compile(r"^(apply( now)?|view( job)?|details|read more|learn more|more)$", re.I)
_JSON_ESCAPE = re.compile(r"\\(?:[bfnrt]|u00[01][0-9a-f])")
_NOISE_HOSTS = re.compile(
    r"(^|\.)(facebook|twitter|x|instagram|youtube|tiktok|pinterest|whatsapp|t)\.(com|me)$", re.I
)
_FILE_PATH = re.compile(r"\.(png|jpe?g|gif|svg|webp|css|js|pdf|docx?|zip|mp4|ico|xml|rss)$", re.I)
_HIDDEN = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.I)
_POSTING_FIELDS = (
    "title", "hiringOrganization", "jobLocation", "jobLocationType", "applicantLocationRequirements",
    "employmentType", "baseSalary", "datePosted", "validThrough", "applicationContact", "description",
)


class CrawlError(Exception):
    """Message is shown to the user."""


@dataclass
class Page:
    url: str
    text: str
    links: list = field(default_factory=list)  # (url, label) pairs
    emails: list = field(default_factory=list)


def normalize_url(url):
    """Same job, same URL: drops fragments, tracking and per-visit parameters."""
    parts = urlsplit(url.strip())
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    has_id = _ID_AT_END.search(parts.path)
    kept = [(k, v) for k, v in pairs if not _TRACKING.match(k) and not (has_id and _PER_VISIT.match(k))]
    query = parts.query if len(kept) == len(pairs) else urlencode(kept)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))


def check_public(url):
    """Only public web addresses: a page must not be able to point us at your router or this app."""
    try:
        parts = urlsplit(url)
        hostname = parts.hostname
    except ValueError as exc:
        raise CrawlError(f"That isn't a web link: {url[:100]}") from exc
    # A backslash makes this check and the HTTP library disagree about which host is meant.
    if parts.scheme not in ("http", "https") or not hostname or "\\" in parts.netloc:
        raise CrawlError(f"That isn't a web link: {url[:100]}")
    try:
        results = socket.getaddrinfo(hostname, parts.port or None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, ValueError) as exc:
        raise CrawlError(f"Couldn't find the website {hostname}.") from exc
    for result in results:
        ip = ipaddress.ip_address(result[4][0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if not ip.is_global:
            raise CrawlError(f"{hostname} isn't a public website.")


class Crawler:
    def __init__(self, delay=1.0):
        self.delay = delay
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.9",
            }
        )
        self._robots = {}
        self._last_request = 0.0

    def fetch(self, url):
        body, charset, final_url = self._download(url)
        page = parse_page(body, final_url, charset)
        if len(page.text) >= MIN_TEXT or page.links:
            return page
        page = self.fetch_rendered(final_url)
        if len(page.text) < MIN_TEXT and not page.links:
            raise CrawlError(
                f"{urlsplit(final_url).hostname} shows almost no text, so it probably "
                f"needs a login. {PASTE_INSTEAD}"
            )
        return page

    def fetch_rendered(self, url):
        """The same page after its JavaScript has run, for boards that are empty without it."""
        check_public(url)
        host = urlsplit(url).hostname
        if not self.allowed(url):
            raise CrawlError(f"{host} asks automated tools not to open this page (robots.txt). {PASTE_INSTEAD}")
        try:
            return parse_page(formfill.render(url), url)
        except PlaywrightError as exc:
            raise CrawlError(f"Couldn't open {host} in Chrome. {PASTE_INSTEAD}") from exc

    def _download(self, url):
        for _ in range(MAX_REDIRECTS + 1):
            check_public(url)
            host = urlsplit(url).hostname
            if not self.allowed(url):
                raise CrawlError(f"{host} asks automated tools not to open this page (robots.txt). {PASTE_INSTEAD}")
            self._wait()
            try:
                response = self.session.get(url, stream=True, timeout=TIMEOUT, allow_redirects=False)
            except requests.RequestException as exc:
                raise CrawlError(f"Couldn't open {host}. The site may be down or slow.") from exc
            with response:
                if response.is_redirect:
                    try:
                        url = urljoin(url, response.headers["location"])
                    except ValueError as exc:
                        raise CrawlError(f"{host} sent a broken redirect.") from exc
                    continue
                status = response.status_code
                if status in (401, 403, 407, 429, 451, 503):
                    raise CrawlError(f"{host} blocked automated access (HTTP {status}). {PASTE_INSTEAD}")
                if status >= 400:
                    raise CrawlError(f"{host} returned HTTP {status} for that link.")
                ctype = response.headers.get("content-type", "").lower()
                if ctype and "html" not in ctype and "text/plain" not in ctype:
                    raise CrawlError(f"That link is not a web page ({ctype.split(';')[0]}). {PASTE_INSTEAD}")
                try:
                    return _read_capped(response), _charset(ctype), url
                except requests.RequestException as exc:
                    raise CrawlError(f"{host} stopped responding while loading the page.") from exc
        raise CrawlError("That link redirects too many times.")

    def allowed(self, url):
        parts = urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        if root not in self._robots:
            parser = RobotFileParser()
            try:
                response = self.session.get(f"{root}/robots.txt", stream=True, timeout=TIMEOUT, allow_redirects=False)
                with response:
                    if response.status_code == 200:
                        parser.parse(_read_capped(response).decode("utf-8", "ignore").splitlines())
                    else:
                        parser.allow_all = True
            except requests.RequestException:
                parser.allow_all = True
            self._robots[root] = parser
        return self._robots[root].can_fetch(ROBOTS_AGENT, url)

    def _wait(self):
        pause = self.delay - (time.monotonic() - self._last_request)
        if pause > 0:
            time.sleep(pause)
        self._last_request = time.monotonic()


def _read_capped(response):
    chunks, size = [], 0
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    for chunk in response.iter_content(65536):
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_BYTES or time.monotonic() > deadline:
            break
    return b"".join(chunks)[:MAX_BYTES]


def _charset(content_type):
    match = re.search(r"charset=[\"']?([\w-]+)", content_type)
    return match.group(1) if match else None


def parse_page(html, url, charset=None):
    if isinstance(html, bytes):
        soup = BeautifulSoup(html, "html.parser", from_encoding=charset)
    else:
        soup = BeautifulSoup(html, "html.parser")
    _reveal_cloudflare_emails(soup)
    postings = _job_postings(soup)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    for tag in soup(["script", "style", "noscript", "template", "svg", "iframe", "head"]):
        tag.decompose()
    # Hidden text is how pages smuggle in spam-trap addresses and instructions for AI tools.
    for tag in soup.find_all(style=_HIDDEN):
        if not tag.decomposed:
            tag.decompose()

    mailtos = []
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if href.lower().startswith("mailto:"):
            mailtos.append(unquote(href[7:]))
    visible = re.sub(r"\n{2,}", "\n", soup.get_text("\n", strip=True))

    sections = [f"Page title: {title}"] if title else []
    if postings:
        # A JSON "\n" before an address would otherwise read as part of it: "ncareers@acme.com".
        data = _JSON_ESCAPE.sub(" ", json.dumps(postings, ensure_ascii=False))
        sections.append(f"Structured job data:\n{data}")
    sections.append(visible)
    if mailtos:
        sections.append("Email links on this page:\n" + "\n".join(mailtos))
    text = "\n\n".join(sections)

    links = _links(soup, url, postings)
    return Page(url=normalize_url(url), text=text, links=links, emails=emails.find_emails(text))


def _reveal_cloudflare_emails(soup):
    """Cloudflare hides addresses from scrapers with a simple XOR. Browsers undo it with JS."""
    for tag in soup.find_all(attrs={"data-cfemail": True}):
        address = _cf_decode(tag["data-cfemail"])
        if address:
            tag.string = address
    for anchor in soup.find_all("a", href=re.compile(r"/cdn-cgi/l/email-protection#")):
        address = _cf_decode(anchor["href"].split("#", 1)[1])
        if address:
            anchor["href"] = f"mailto:{address}"
            anchor.string = address


def _cf_decode(encoded):
    try:
        key = int(encoded[:2], 16)
        return "".join(chr(int(encoded[i:i + 2], 16) ^ key) for i in range(2, len(encoded), 2))
    except ValueError:
        return ""


def _job_postings(soup):
    """schema.org JobPosting data: often the only job text in a JavaScript-heavy page."""
    found = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            # The round trip drops half-emojis (lone surrogates), which can't be stored later.
            data = json.dumps(json.loads(tag.string or ""), ensure_ascii=False).encode("utf-8", "ignore")
            _collect_postings(json.loads(data), found)
        except (ValueError, RecursionError):
            continue
    many = len(found) > 1
    cleaned = []
    for posting in found[:30]:
        item = {key: posting[key] for key in _POSTING_FIELDS if key in posting}
        if isinstance(item.get("description"), str):
            description = BeautifulSoup(item["description"], "html.parser").get_text("\n", strip=True)
            item["description"] = description[:300] if many else description
        if isinstance(posting.get("url"), str):
            item["url"] = posting["url"]
        cleaned.append(item)
    return cleaned


def _collect_postings(node, found):
    if isinstance(node, list):
        for item in node:
            _collect_postings(item, found)
    elif isinstance(node, dict):
        kind = node.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            found.append(node)
        for key in ("@graph", "itemListElement", "item"):
            if key in node:
                _collect_postings(node[key], found)


def _links(soup, base, postings):
    seen = {}
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith("#") or href.lower().startswith(("mailto:", "tel:", "javascript:", "data:")):
            continue
        label = " ".join(anchor.get_text(" ", strip=True).split())[:120]
        label = label or anchor.get("aria-label") or anchor.get("title") or ""
        if label:
            _add_link(seen, base, href, label)
    for posting in postings:
        if posting.get("url"):
            _add_link(seen, base, posting["url"], str(posting.get("title") or "Job posting")[:120])
    seen.pop(normalize_url(base), None)
    return list(seen.items())[:MAX_LINKS]


def _add_link(seen, base, href, label):
    try:
        url = normalize_url(urljoin(base, href))
        parts = urlsplit(url)
    except ValueError:
        return
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return
    if _NOISE_HOSTS.search(parts.hostname) or _FILE_PATH.search(parts.path):
        return
    # Job cards often have an "Apply" link before the one holding the job's title.
    if url not in seen or _GENERIC_LABEL.match(seen[url]):
        seen[url] = label
