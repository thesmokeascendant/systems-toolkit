#!/usr/bin/env python3
"""
web_scraper.py — a polite, defensive web scraper.

Separates *fetching* (network I/O, retries, robots.txt, rate limiting)
from *parsing* (HTML → structured data) so the parsing logic can be unit
tested against fixture HTML without any network access, while the fetch
logic is exercised against a real local HTTP server in integration tests.

Usage:
    from web_scraper import fetch_page, extract_links, extract_text

    page = fetch_page("https://example.com")
    links = extract_links(page.soup, base_url=page.url)

CLI:
    ./web_scraper.py https://example.com --links
    ./web_scraper.py https://example.com --selector "h1, h2"
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


class ScraperError(Exception):
    """Base class for all scraper-raised errors."""


class FetchError(ScraperError):
    """The page could not be retrieved (network failure, timeout, bad status)."""


class RobotsDisallowedError(ScraperError):
    """robots.txt disallows fetching this URL for our user agent."""


class UnsupportedContentTypeError(ScraperError):
    """The response wasn't HTML — nothing to parse."""

    def __init__(self, content_type: str):
        self.content_type = content_type
        super().__init__(f"unsupported content type: {content_type!r}")


@dataclass
class Page:
    url: str
    status_code: int
    html: str
    soup: BeautifulSoup = field(repr=False)


class PoliteRateLimiter:
    """
    Enforces a minimum delay between requests to the *same* host, so a
    scraping loop over many pages on one site doesn't hammer it. Different
    hosts are not throttled against each other.
    """

    def __init__(self, min_delay: float = 1.0, sleep_fn=time.sleep, clock=time.monotonic):
        self.min_delay = min_delay
        self._sleep = sleep_fn
        self._clock = clock
        self._last_request_at: dict[str, float] = {}

    def wait_if_needed(self, url: str) -> None:
        host = urlparse(url).netloc
        now = self._clock()
        last = self._last_request_at.get(host)
        if last is not None:
            elapsed = now - last
            if elapsed < self.min_delay:
                self._sleep(self.min_delay - elapsed)
        self._last_request_at[host] = self._clock()


class RobotsChecker:
    """
    Checks robots.txt before fetching. Fails open (allows the fetch) if
    robots.txt can't be retrieved at all — a missing robots.txt means no
    restrictions, per the standard.
    """

    def __init__(self, session: Optional[requests.Session] = None, user_agent: str = "*"):
        self.session = session or requests.Session()
        self.user_agent = user_agent
        self._parsers: dict[str, urllib.robotparser.RobotFileParser] = {}

    def is_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._parsers:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = urljoin(origin, "/robots.txt")
            try:
                resp = self.session.get(robots_url, timeout=5)
                if resp.status_code == 200:
                    rp.parse(resp.text.splitlines())
                else:
                    rp.parse([])  # no robots.txt → allow everything
            except requests.exceptions.RequestException:
                rp.parse([])  # unreachable → fail open
            self._parsers[origin] = rp
        return self._parsers[origin].can_fetch(self.user_agent, url)


def fetch_page(
    url: str,
    timeout: float = 10.0,
    max_retries: int = 3,
    session: Optional[requests.Session] = None,
    rate_limiter: Optional[PoliteRateLimiter] = None,
    robots_checker: Optional[RobotsChecker] = None,
    user_agent: str = "systems-toolkit-scraper/1.0",
) -> Page:
    """Fetch and parse a single page, honoring robots.txt and rate limiting."""
    session = session or requests.Session()

    if robots_checker and not robots_checker.is_allowed(url):
        raise RobotsDisallowedError(f"robots.txt disallows fetching {url}")

    if rate_limiter:
        rate_limiter.wait_if_needed(url)

    last_exception: Optional[Exception] = None
    for attempt in range(max_retries + 1):
        try:
            response = session.get(
                url, timeout=timeout, headers={"User-Agent": user_agent}
            )
        except requests.exceptions.Timeout as e:
            last_exception = FetchError(f"timed out fetching {url}: {e}")
        except requests.exceptions.ConnectionError as e:
            last_exception = FetchError(f"could not reach {url}: {e}")
        else:
            if response.status_code >= 500 and attempt < max_retries:
                time.sleep(min(0.5 * (2 ** attempt), 8.0))
                continue
            if response.status_code >= 400:
                raise FetchError(f"HTTP {response.status_code} fetching {url}")

            content_type = response.headers.get("Content-Type", "")
            if "html" not in content_type and content_type != "":
                raise UnsupportedContentTypeError(content_type)

            soup = BeautifulSoup(response.text, "html.parser")
            return Page(url=url, status_code=response.status_code, html=response.text, soup=soup)

        if attempt < max_retries:
            time.sleep(min(0.5 * (2 ** attempt), 8.0))
            continue
        raise last_exception

    raise last_exception or FetchError(f"failed to fetch {url} for an unknown reason")


def extract_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """
    Extract all hyperlinks, resolved to absolute URLs, deduplicated,
    preserving first-seen order. Skips javascript:/mailto:/tel: links and
    anchors with no href at all.
    """
    seen: set[str] = set()
    links: list[str] = []
    for tag in soup.find_all("a"):
        href = tag.get("href")
        if not href:
            continue
        href = href.strip()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        absolute = urljoin(base_url, href)
        if absolute not in seen:
            seen.add(absolute)
            links.append(absolute)
    return links


def extract_text(soup: BeautifulSoup, selector: str) -> list[str]:
    """
    Extract stripped text content from every element matching a CSS
    selector. Returns an empty list (not an error) if nothing matches —
    a missing element is expected scraper behavior, not exceptional.
    """
    return [el.get_text(strip=True) for el in soup.select(selector) if el.get_text(strip=True)]


def extract_table(soup: BeautifulSoup, selector: str = "table") -> list[dict[str, str]]:
    """
    Extract the first matching <table> as a list of row dicts keyed by
    header text. Handles missing <thead>, ragged rows (fewer/more cells
    than headers), and empty tables without raising.
    """
    table = soup.select_one(selector)
    if table is None:
        return []

    rows = table.find_all("tr")
    if not rows:
        return []

    header_cells = rows[0].find_all(["th", "td"])
    headers = [c.get_text(strip=True) or f"col_{i}" for i, c in enumerate(header_cells)]

    results = []
    for row in rows[1:]:
        cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
        record = {}
        for i, header in enumerate(headers):
            record[header] = cells[i] if i < len(cells) else None
        results.append(record)
    return results


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polite, defensive web scraper")
    parser.add_argument("url")
    parser.add_argument("--selector", help="CSS selector to extract text from")
    parser.add_argument("--links", action="store_true", help="Extract all links instead")
    parser.add_argument("--table", action="store_true", help="Extract the first table as JSON")
    parser.add_argument("--respect-robots", action="store_true")
    parser.add_argument("--delay", type=float, default=1.0, help="Min seconds between requests")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    robots_checker = RobotsChecker() if args.respect_robots else None
    rate_limiter = PoliteRateLimiter(min_delay=args.delay)

    try:
        page = fetch_page(args.url, rate_limiter=rate_limiter, robots_checker=robots_checker)
    except RobotsDisallowedError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except UnsupportedContentTypeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except FetchError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.links:
        for link in extract_links(page.soup, base_url=page.url):
            print(link)
    elif args.table:
        import json

        print(json.dumps(extract_table(page.soup), indent=2))
    elif args.selector:
        for text in extract_text(page.soup, args.selector):
            print(text)
    else:
        title = page.soup.title.get_text(strip=True) if page.soup.title else "(no title)"
        print(f"{title}\n{len(extract_links(page.soup, page.url))} links found")

    return 0


if __name__ == "__main__":
    sys.exit(main())
