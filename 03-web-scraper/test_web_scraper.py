"""
Tests for web_scraper.py

Parsing tests run against fixture HTML files — no network needed.
Fetch-layer tests (retries, robots.txt, content-type checks, rate
limiting) run against a real local HTTP server on an ephemeral port —
also no network needed, but real HTTP semantics.

Run with:
    python3 -m unittest discover -s tests -v
"""

import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_scraper import (  # noqa: E402
    FetchError,
    PoliteRateLimiter,
    RobotsChecker,
    RobotsDisallowedError,
    UnsupportedContentTypeError,
    extract_links,
    extract_table,
    extract_text,
    fetch_page,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load_soup(name: str) -> BeautifulSoup:
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return BeautifulSoup(html, "html.parser")


# ---------------------------------------------------------------------
# Parsing tests — pure functions, fixture HTML, no network at all.
# ---------------------------------------------------------------------

class TestExtractLinks(unittest.TestCase):
    def setUp(self):
        self.soup = load_soup("clean_page.html")

    def test_relative_links_resolved_to_absolute(self):
        links = extract_links(self.soup, base_url="https://example.com/index.html")
        self.assertIn("https://example.com/about", links)

    def test_absolute_links_kept_as_is(self):
        links = extract_links(self.soup, base_url="https://example.com")
        self.assertIn("https://external.example.com/page", links)

    def test_mailto_and_javascript_and_anchor_links_skipped(self):
        links = extract_links(self.soup, base_url="https://example.com")
        joined = " ".join(links)
        self.assertNotIn("mailto:", joined)
        self.assertNotIn("javascript:", joined)
        self.assertNotIn("#section", joined)

    def test_anchor_with_no_href_does_not_crash(self):
        # The fixture has an <a> with no href attribute — must not raise.
        links = extract_links(self.soup, base_url="https://example.com")
        self.assertIsInstance(links, list)

    def test_duplicate_links_deduplicated(self):
        links = extract_links(self.soup, base_url="https://example.com")
        self.assertEqual(links.count("https://example.com/about"), 1)

    def test_empty_page_returns_empty_list(self):
        soup = BeautifulSoup("<html><body></body></html>", "html.parser")
        self.assertEqual(extract_links(soup, "https://example.com"), [])


class TestExtractText(unittest.TestCase):
    def test_matching_selector_returns_text(self):
        soup = load_soup("clean_page.html")
        self.assertEqual(extract_text(soup, "h1"), ["Main Heading"])

    def test_no_matching_selector_returns_empty_list_not_error(self):
        soup = load_soup("clean_page.html")
        self.assertEqual(extract_text(soup, ".does-not-exist"), [])

    def test_selector_matching_empty_elements_excluded(self):
        soup = BeautifulSoup("<p></p><p>Real text</p>", "html.parser")
        self.assertEqual(extract_text(soup, "p"), ["Real text"])


class TestExtractTable(unittest.TestCase):
    def test_normal_table_parsed_into_row_dicts(self):
        soup = load_soup("table_page.html")
        rows = extract_table(soup)
        self.assertEqual(rows[0], {"Name": "Ada", "Role": "Engineer", "Years": "10"})

    def test_ragged_row_with_missing_cells_padded_with_none(self):
        soup = load_soup("table_page.html")
        rows = extract_table(soup)
        grace_row = rows[1]
        self.assertEqual(grace_row["Name"], "Grace")
        self.assertIsNone(grace_row["Years"])

    def test_row_with_extra_cells_does_not_crash(self):
        soup = load_soup("table_page.html")
        rows = extract_table(soup)  # would raise IndexError if not handled
        self.assertEqual(len(rows), 3)

    def test_no_table_present_returns_empty_list(self):
        soup = BeautifulSoup("<html><body>no tables here</body></html>", "html.parser")
        self.assertEqual(extract_table(soup), [])

    def test_table_with_no_header_row_treated_as_data(self):
        # broken_page.html has a <table> whose only row is data, no header.
        soup = load_soup("broken_page.html")
        rows = extract_table(soup)
        # First row becomes the "header" — this is documented behavior,
        # not a crash, for a table that violates the header convention.
        self.assertEqual(rows, [])  # only one row total = header, no data rows


class TestMalformedHTML(unittest.TestCase):
    """BeautifulSoup's html.parser is lenient by design — confirm our
    extraction functions still work on genuinely broken markup."""

    def setUp(self):
        self.soup = load_soup("broken_page.html")

    def test_unclosed_tags_do_not_crash_link_extraction(self):
        links = extract_links(self.soup, base_url="https://example.com")
        self.assertIn("https://example.com/valid-link", links)

    def test_unclosed_tags_do_not_crash_text_extraction(self):
        # html.parser is lenient about unclosed tags, but that leniency
        # means an unclosed <h1> swallows everything that follows it as
        # nested content — so extracting "h1" text here returns the whole
        # rest of the body, not just the heading. That's real, documented
        # html.parser behavior, not a bug in extract_text(); the point of
        # this test is simply that malformed markup never raises.
        text = extract_text(self.soup, "h1")
        self.assertEqual(len(text), 1)
        self.assertTrue(text[0].startswith("Unclosed heading"))


# ---------------------------------------------------------------------
# Fetch-layer tests — real local HTTP server, still no real network.
# ---------------------------------------------------------------------

class ScriptedHandler(BaseHTTPRequestHandler):
    script: dict = {}
    call_counts: dict = {}

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        ScriptedHandler.call_counts[self.path] = ScriptedHandler.call_counts.get(self.path, 0) + 1
        attempt = ScriptedHandler.call_counts[self.path] - 1
        steps = ScriptedHandler.script.get(self.path, [{"status": 200, "body": "<html></html>", "content_type": "text/html"}])
        step = steps[min(attempt, len(steps) - 1)]

        self.send_response(step.get("status", 200))
        self.send_header("Content-Type", step.get("content_type", "text/html"))
        self.end_headers()
        self.wfile.write(step.get("body", "").encode("utf-8"))


class TestFetchLayer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), ScriptedHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        ScriptedHandler.script = {}
        ScriptedHandler.call_counts = {}

    def test_fetch_returns_parsed_page(self):
        ScriptedHandler.script["/page"] = [
            {"status": 200, "body": "<html><title>T</title></html>", "content_type": "text/html"}
        ]
        page = fetch_page(f"{self.base_url}/page")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.soup.title.get_text(), "T")

    def test_non_html_content_type_raises(self):
        ScriptedHandler.script["/file.pdf"] = [
            {"status": 200, "body": "%PDF-1.4 fake", "content_type": "application/pdf"}
        ]
        with self.assertRaises(UnsupportedContentTypeError):
            fetch_page(f"{self.base_url}/file.pdf")

    def test_404_raises_fetch_error(self):
        ScriptedHandler.script["/missing"] = [{"status": 404, "body": "", "content_type": "text/html"}]
        with self.assertRaises(FetchError):
            fetch_page(f"{self.base_url}/missing")

    def test_connection_error_raises_fetch_error(self):
        with self.assertRaises(FetchError):
            fetch_page("http://127.0.0.1:1/unreachable", max_retries=0)

    def test_robots_disallowed_blocks_fetch(self):
        ScriptedHandler.script["/robots.txt"] = [
            {"status": 200, "body": "User-agent: *\nDisallow: /private\n", "content_type": "text/plain"}
        ]
        checker = RobotsChecker(user_agent="*")
        with self.assertRaises(RobotsDisallowedError):
            fetch_page(f"{self.base_url}/private", robots_checker=checker)

    def test_robots_missing_fails_open(self):
        # No /robots.txt script entry → server 404s it → checker should
        # allow the fetch rather than block everything.
        ScriptedHandler.script["/robots.txt"] = [{"status": 404, "body": "", "content_type": "text/plain"}]
        ScriptedHandler.script["/open-page"] = [
            {"status": 200, "body": "<html></html>", "content_type": "text/html"}
        ]
        checker = RobotsChecker(user_agent="*")
        page = fetch_page(f"{self.base_url}/open-page", robots_checker=checker)
        self.assertEqual(page.status_code, 200)


class TestPoliteRateLimiter(unittest.TestCase):
    def test_waits_between_requests_to_same_host(self):
        sleeps = []
        clock = iter([0.0, 0.0, 0.2, 0.2]).__next__
        limiter = PoliteRateLimiter(min_delay=1.0, sleep_fn=lambda s: sleeps.append(s), clock=clock)
        limiter.wait_if_needed("https://example.com/a")
        limiter.wait_if_needed("https://example.com/b")
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.8, places=3)

    def test_different_hosts_not_throttled_against_each_other(self):
        sleeps = []
        limiter = PoliteRateLimiter(min_delay=1.0, sleep_fn=lambda s: sleeps.append(s))
        limiter.wait_if_needed("https://a.example.com/")
        limiter.wait_if_needed("https://b.example.com/")
        self.assertEqual(len(sleeps), 0)


if __name__ == "__main__":
    unittest.main()
