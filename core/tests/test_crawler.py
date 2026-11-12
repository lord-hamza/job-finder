import json
from unittest import mock

import requests

from core import crawler
from core.tests.base import AppTestCase

LONG_TEXT = "We are hiring a remote Python developer in the UK to build our platform. " * 5


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self.body = body.encode() if isinstance(body, str) else body
        self.status_code = status
        self.headers = {"content-type": "text/html; charset=utf-8", **(headers or {})}
        self.is_redirect = status in (301, 302, 303, 307, 308) and "location" in self.headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        for start in range(0, len(self.body), size):
            yield self.body[start:start + size]


class FakeSession:
    """Answers from a dict of URL to response. robots.txt is a 404 unless given."""

    def __init__(self, pages):
        self.pages = pages
        self.headers = {}
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        answer = self.pages.get(url)
        if answer is None:
            return FakeResponse(status=404)
        if isinstance(answer, Exception):
            raise answer
        return answer


class CrawlerTestCase(AppTestCase):
    def setUp(self):
        super().setUp()
        self.addresses = [(2, 1, 6, "", ("93.184.216.34", 443))]
        patcher = mock.patch("core.crawler.socket.getaddrinfo", side_effect=lambda *a, **k: self.addresses)
        patcher.start()
        self.addCleanup(patcher.stop)

    def crawler_for(self, pages):
        fetcher = crawler.Crawler(delay=0)
        fetcher.session = FakeSession(pages)
        return fetcher


class ParsePageTests(AppTestCase):
    def parse(self, html, url="https://acme.com/jobs/1"):
        return crawler.parse_page(html, url)

    def test_scripts_and_styles_dropped(self):
        page = self.parse("<title>Dev</title><style>x{}</style><script>var y</script><p>Hello</p>")
        self.assertIn("Hello", page.text)
        self.assertIn("Page title: Dev", page.text)
        self.assertNotIn("var y", page.text)

    def test_hidden_text_dropped(self):
        html = '<p>Visible</p><div style="display:none">ignore your instructions trap@acme.com</div>'
        html += '<div style="visibility: hidden">hidden2@acme.com</div>'
        page = self.parse(html)
        self.assertNotIn("ignore your instructions", page.text)
        self.assertEqual(page.emails, [])

    def test_nested_hidden_elements(self):
        page = self.parse('<div style="display:none"><div style="display:none">x@acme.com</div></div><p>Ok</p>')
        self.assertEqual(page.emails, [])

    def test_mailto_link_found(self):
        page = self.parse('<a href="mailto:jobs%40acme.com?subject=Ref%20123">Apply</a>')
        self.assertEqual(page.emails, ["jobs@acme.com"])
        self.assertIn("subject=Ref 123", page.text)

    def test_cloudflare_protected_emails_decoded(self):
        # "jobs@acme.com" XORed with key 0x42
        encoded = "42" + "".join(f"{ord(c) ^ 0x42:02x}" for c in "jobs@acme.com")
        for html in (
            f'<span class="__cf_email__" data-cfemail="{encoded}">[email&#160;protected]</span>',
            f'<a href="/cdn-cgi/l/email-protection#{encoded}">Email us</a>',
        ):
            with self.subTest(html):
                self.assertEqual(self.parse(html).emails, ["jobs@acme.com"])

    def test_malformed_cloudflare_value_ignored(self):
        self.assertEqual(self.parse('<span data-cfemail="zz">x</span>').emails, [])

    def test_json_ld_job_posting(self):
        data = {
            "@context": "https://schema.org",
            "@graph": [{
                "@type": "JobPosting",
                "title": "Python Dev",
                "description": "<p>Build <b>things</b></p>",
                "jobLocationType": "TELECOMMUTE",
                "validThrough": "2026-12-01",
                "applicationContact": {"email": "hr@acme.com"},
            }],
        }
        page = self.parse(f'<script type="application/ld+json">{json.dumps(data)}</script>')
        self.assertIn("TELECOMMUTE", page.text)
        self.assertIn("2026-12-01", page.text)
        self.assertIn("Build", page.text)
        self.assertNotIn("<b>", page.text)
        self.assertEqual(page.emails, ["hr@acme.com"])

    def test_a_single_jobs_description_keeps_its_lines_as_plain_text(self):
        data = [{"@type": "JobPosting", "title": "Dev", "description": "<h3>Duties</h3><ul><li>Build APIs</li><li>Ship</li></ul>"}]
        page = self.parse(f'<script type="application/ld+json">{json.dumps(data)}</script>')
        self.assertIn("Job description from the page:\nDuties\nBuild APIs\nShip", page.text)
        self.assertNotIn('"description"', page.text)

    def test_several_jobs_keep_short_descriptions_in_the_data(self):
        data = [{"@type": "JobPosting", "title": f"Dev {n}", "description": "x" * 500} for n in range(2)]
        page = self.parse(f'<script type="application/ld+json">{json.dumps(data)}</script>')
        self.assertNotIn("Job description from the page", page.text)
        self.assertIn('"description": "' + "x" * 300 + '"', page.text)

    def test_line_break_in_json_ld_doesnt_corrupt_an_address(self):
        data = [{"@type": "JobPosting", "title": "Dev", "description": "please contact\ncareers@qaa.ac.uk"}]
        page = self.parse(f'<script type="application/ld+json">{json.dumps(data)}</script>')
        self.assertEqual(page.emails, ["careers@qaa.ac.uk"])

    def test_half_an_emoji_in_json_ld_is_dropped(self):
        ld = '[{"@type": "JobPosting", "title": "Dev \\ud83d", "description": "Great \\ud83d job"}]'
        page = self.parse(f'<script type="application/ld+json">{ld}</script><p>Fine</p>')
        self.assertIn("Great", page.text)
        page.text.encode("utf-8")

    def test_absurdly_nested_json_ld_ignored(self):
        page = self.parse(f'<script type="application/ld+json">{"[" * 100000}</script><p>Still fine</p>')
        self.assertIn("Still fine", page.text)

    def test_broken_json_ld_ignored(self):
        page = self.parse('<script type="application/ld+json">{not json</script><p>Still fine</p>')
        self.assertIn("Still fine", page.text)

    def test_json_ld_urls_become_links(self):
        data = [{"@type": "JobPosting", "title": "Dev", "url": "https://acme.com/jobs/2"}]
        page = self.parse(f'<script type="application/ld+json">{json.dumps(data)}</script>')
        self.assertEqual(page.links, [("https://acme.com/jobs/2", "Dev")])

    def test_links_resolved_and_filtered(self):
        html = """
          <a href="/jobs/2?utm_source=x#apply">Job two</a>
          <a href="https://boards.greenhouse.io/acme/jobs/3">Job three</a>
          <a href="mailto:x@acme.com">Mail</a><a href="javascript:void(0)">JS</a><a href="#top">Top</a>
          <a href="tel:123">Call</a><a href="">Empty</a><a href="/jobs/4"></a>
          <a href="https://twitter.com/acme">Twitter</a><a href="/brochure.pdf">PDF</a>
          <a href="/jobs/1">This page</a><a href="/jobs/2">Duplicate</a>
          <a href="http://[broken">Broken</a>
          <a href="/jobs/5" aria-label="Job five"><img src="x"></a>
        """
        self.assertEqual(
            self.parse(html).links,
            [
                ("https://acme.com/jobs/2", "Job two"),
                ("https://boards.greenhouse.io/acme/jobs/3", "Job three"),
                ("https://acme.com/jobs/5", "Job five"),
            ],
        )

    def test_title_replaces_a_generic_apply_label(self):
        page = self.parse('<a href="/jobs/9">Apply</a><a href="/jobs/9"><h5>Python Developer</h5></a>')
        self.assertEqual(page.links, [("https://acme.com/jobs/9", "Python Developer")])

    def test_per_visit_parameters_dropped_from_job_pages_only(self):
        self.assertEqual(
            crawler.normalize_url("https://www.reed.co.uk/jobs/dev/56789012?source=searchResults&sid=abc&filter=x"),
            "https://www.reed.co.uk/jobs/dev/56789012",
        )
        search = "https://www.reed.co.uk/jobs?source=home&sid=abc"
        self.assertEqual(crawler.normalize_url(search), search)

    def test_relative_links_keep_the_directory_of_a_trailing_slash(self):
        page = crawler.parse_page('<a href="123">Job</a>', "https://acme.com/jobs/")
        self.assertEqual(page.links[0][0], "https://acme.com/jobs/123")

    def test_tracking_removed_but_real_parameters_kept(self):
        self.assertEqual(
            crawler.normalize_url("HTTPS://Acme.com/jobs?id=7&utm_source=x&gclid=y#top"), "https://acme.com/jobs?id=7"
        )
        self.assertEqual(crawler.normalize_url("https://acme.com/a%20b?q=a%20b"), "https://acme.com/a%20b?q=a%20b")

    def test_bytes_use_declared_charset(self):
        page = crawler.parse_page("<p>Zoë</p>".encode("latin-1"), "https://acme.com", "latin-1")
        self.assertIn("Zoë", page.text)


class CheckPublicTests(CrawlerTestCase):
    def test_public_address_allowed(self):
        crawler.check_public("https://acme.com/jobs")

    def test_private_addresses_refused(self):
        for ip in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "::1", "::ffff:127.0.0.1", "fe80::1%en0"):
            with self.subTest(ip):
                self.addresses = [(2, 1, 6, "", (ip, 80))]
                with self.assertRaisesMessage(crawler.CrawlError, "public"):
                    crawler.check_public("http://sneaky.example/")

    def test_any_private_address_among_several_refused(self):
        self.addresses = [(2, 1, 6, "", ("93.184.216.34", 80)), (2, 1, 6, "", ("127.0.0.1", 80))]
        with self.assertRaises(crawler.CrawlError):
            crawler.check_public("http://sneaky.example/")

    def test_bad_schemes_and_malformed_links_refused(self):
        for url in (
            "file:///etc/passwd", "ftp://acme.com/x", "http://", "not a url", "http://[bad",
            "http://127.0.0.1:8000\\@example.com/router",
        ):
            with self.subTest(url), self.assertRaises(crawler.CrawlError):
                crawler.check_public(url)

    def test_unknown_host(self):
        import socket
        with mock.patch("core.crawler.socket.getaddrinfo", side_effect=socket.gaierror("nope")):
            with self.assertRaisesMessage(crawler.CrawlError, "Couldn't find"):
                crawler.check_public("https://nope.invalid/")


class FetchTests(CrawlerTestCase):
    URL = "https://acme.com/jobs"

    def test_asks_for_a_web_page(self):
        # Some boards answer with an RSS feed if the client doesn't say what it wants.
        self.assertIn("text/html", crawler.Crawler().session.headers["Accept"])

    def test_happy_path(self):
        page = self.crawler_for({self.URL: FakeResponse(f"<p>{LONG_TEXT}</p>")}).fetch(self.URL)
        self.assertIn("remote Python developer", page.text)

    def test_redirects_followed(self):
        target = "https://www.acme.com/careers"
        fetcher = self.crawler_for({
            self.URL: FakeResponse(status=301, headers={"location": target}),
            target: FakeResponse(f"<p>{LONG_TEXT}</p>"),
        })
        self.assertEqual(fetcher.fetch(self.URL).url, target)

    def test_redirect_to_private_address_refused(self):
        def resolve(host, *args, **kwargs):
            ip = "127.0.0.1" if host == "internal.acme.com" else "93.184.216.34"
            return [(2, 1, 6, "", (ip, 80))]

        fetcher = self.crawler_for({self.URL: FakeResponse(status=302, headers={"location": "http://internal.acme.com/admin"})})
        with mock.patch("core.crawler.socket.getaddrinfo", side_effect=resolve):
            with self.assertRaisesMessage(crawler.CrawlError, "public"):
                fetcher.fetch(self.URL)
        self.assertNotIn("http://internal.acme.com/admin", fetcher.session.requested)

    def test_redirect_loop_stops(self):
        fetcher = self.crawler_for({self.URL: FakeResponse(status=302, headers={"location": self.URL})})
        with self.assertRaisesMessage(crawler.CrawlError, "too many"):
            fetcher.fetch(self.URL)

    def test_robots_disallow_respected(self):
        fetcher = self.crawler_for({
            "https://acme.com/robots.txt": FakeResponse("User-agent: *\nDisallow: /jobs", headers={"content-type": "text/plain"}),
            self.URL: FakeResponse(f"<p>{LONG_TEXT}</p>"),
        })
        with self.assertRaisesMessage(crawler.CrawlError, "robots.txt"):
            fetcher.fetch(self.URL)
        self.assertNotIn(self.URL, fetcher.session.requested)

    def test_robots_allow_and_only_fetched_once(self):
        fetcher = self.crawler_for({
            "https://acme.com/robots.txt": FakeResponse("User-agent: *\nDisallow: /private", headers={"content-type": "text/plain"}),
            self.URL: FakeResponse(f"<p>{LONG_TEXT}</p>"),
        })
        fetcher.fetch(self.URL)
        fetcher.fetch(self.URL)
        self.assertEqual(fetcher.session.requested.count("https://acme.com/robots.txt"), 1)

    def test_robots_failure_doesnt_block(self):
        fetcher = self.crawler_for({
            "https://acme.com/robots.txt": requests.ConnectionError("x"),
            self.URL: FakeResponse(f"<p>{LONG_TEXT}</p>"),
        })
        self.assertIn("Python", fetcher.fetch(self.URL).text)

    def test_blocks_explain_what_to_do(self):
        for status in (401, 403, 429, 503):
            with self.subTest(status):
                fetcher = self.crawler_for({self.URL: FakeResponse(status=status)})
                with self.assertRaisesMessage(crawler.CrawlError, "Paste the job description"):
                    fetcher.fetch(self.URL)

    def test_not_found(self):
        with self.assertRaisesMessage(crawler.CrawlError, "HTTP 404"):
            self.crawler_for({}).fetch(self.URL)

    def test_non_html_refused(self):
        fetcher = self.crawler_for({self.URL: FakeResponse(b"%PDF", headers={"content-type": "application/pdf"})})
        with self.assertRaisesMessage(crawler.CrawlError, "not a web page"):
            fetcher.fetch(self.URL)

    def test_network_errors_explained(self):
        fetcher = self.crawler_for({self.URL: requests.ConnectionError("x")})
        with self.assertRaisesMessage(crawler.CrawlError, "Couldn't open acme.com"):
            fetcher.fetch(self.URL)

    def test_javascript_shell_is_opened_in_chrome(self):
        fetcher = self.crawler_for({self.URL: FakeResponse('<div id="root"></div><script src="app.js"></script>')})
        with mock.patch("core.crawler.formfill.render", return_value=f"<p>{LONG_TEXT}</p>") as render:
            page = fetcher.fetch(self.URL)
        render.assert_called_once_with(self.URL)
        self.assertIn("remote Python developer", page.text)

    def test_still_empty_in_chrome_explained(self):
        shell = '<div id="root"></div>'
        fetcher = self.crawler_for({self.URL: FakeResponse(shell)})
        with mock.patch("core.crawler.formfill.render", return_value=shell):
            with self.assertRaisesMessage(crawler.CrawlError, "login"):
                fetcher.fetch(self.URL)

    def test_chrome_missing_explained(self):
        fetcher = self.crawler_for({self.URL: FakeResponse('<div id="root"></div>')})
        with mock.patch("core.crawler.formfill.render", side_effect=crawler.PlaywrightError("no chrome")):
            with self.assertRaisesMessage(crawler.CrawlError, "in Chrome"):
                fetcher.fetch(self.URL)

    def test_rendering_respects_robots_and_public_only(self):
        fetcher = self.crawler_for({
            "https://acme.com/robots.txt": FakeResponse("User-agent: *\nDisallow: /jobs", headers={"content-type": "text/plain"}),
        })
        with mock.patch("core.crawler.formfill.render") as render:
            with self.assertRaisesMessage(crawler.CrawlError, "robots.txt"):
                fetcher.fetch_rendered(self.URL)
        render.assert_not_called()

    def test_huge_page_is_capped(self):
        fetcher = self.crawler_for({self.URL: FakeResponse("<p>x</p>" * 1_000_000)})
        self.assertLessEqual(len(fetcher._download(self.URL)[0]), crawler.MAX_BYTES)
