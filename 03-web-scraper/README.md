# 03 — Web Scraper

A polite, defensive web scraper built on `requests` + BeautifulSoup, with
fetching (network, retries, robots.txt, rate limiting) cleanly separated
from parsing (HTML → structured data) so each half can be tested for what
it actually does.

## Problem

Scraper code that's just `requests.get(url)` piped into BeautifulSoup
breaks in predictable ways: real-world HTML is malformed, some responses
aren't HTML at all, relative links need resolving, sites expect scrapers
to respect `robots.txt` and not hammer them, and a page with no matching
elements shouldn't be treated as an error.

## Requirements

- Fetch pages defensively: retry transient failures, fail cleanly on 4xx,
  detect non-HTML content types before trying to parse them as HTML.
- Respect `robots.txt` when asked to (fail open if it's unreachable —
  that's the correct interpretation of a missing robots.txt).
- Rate-limit requests per-host so a scrape loop doesn't hammer one site.
- Parse HTML defensively: resolve relative links to absolute, deduplicate,
  skip non-navigable hrefs (`javascript:`, `mailto:`, `tel:`, bare `#`).
- Extract tables even when rows are ragged (missing or extra cells).
- Never crash on genuinely malformed markup (unclosed tags, missing
  header rows, empty documents).
- Be testable without live network access.

## Architecture

```
web_scraper.py
├── PoliteRateLimiter     # per-host minimum delay between requests
├── RobotsChecker         # robots.txt fetch + can_fetch(), fails open
├── fetch_page()          # network layer: retries, status/content-type checks
├── extract_links()       # HTML → absolute, deduped, navigable URLs
├── extract_text()        # HTML → stripped text for a CSS selector
└── extract_table()       # HTML → list of row dicts, ragged-row safe
```

Fetching and parsing don't know about each other beyond `Page` (a small
dataclass: url, status, raw html, parsed soup). That boundary is what lets
`extract_*` be tested purely against fixture HTML files with zero network
involvement, while `fetch_page` is tested against a real local server.

## Design Decisions

- **robots.txt fails open, not closed.** Per the robots exclusion
  standard, no robots.txt means no restrictions. Treating an unreachable
  robots.txt as "disallow everything" would make the scraper unusable
  against any site with transient robots.txt issues, which isn't what the
  standard specifies.
- **Content-Type is checked before parsing.** A PDF or image served at a
  URL that looks like a page shouldn't get force-fed into BeautifulSoup —
  raising `UnsupportedContentTypeError` up front is more honest than
  silently parsing garbage and returning an empty result set.
- **Rate limiting is per-host, not global.** A scraper working across
  multiple sites shouldn't slow down against site B because it just hit
  site A — the politeness contract is with each host individually.
- **`extract_table` pads short rows with `None` rather than raising.**
  Real-world tables (especially scraped from CMSes) are frequently ragged.
  Losing the whole table because one row has a missing `<td>` is worse
  than returning `None` for the missing field.

## Algorithms Used

- `urllib.parse.urljoin` for relative → absolute URL resolution.
- Order-preserving deduplication via a `set` + list scan.
- `urllib.robotparser.RobotFileParser` for robots.txt rule evaluation.
- Exponential backoff (shared pattern with the API client project) for
  transient 5xx responses during fetch.

## Tradeoffs

- `html.parser` (stdlib) is used instead of `lxml` to avoid an extra
  system dependency. It's slower on very large documents and — as the
  test suite documents — has real leniency quirks with unclosed tags
  (see Lessons Learned). `lxml` would be the production choice for a
  scraper handling high volume.
- `extract_table` only extracts the *first* matching table. A page with
  multiple tables needs `soup.select("table")` and a loop at the call
  site — kept the function single-purpose rather than guessing which
  table the caller wants.

## Edge Cases Handled

| Case | Behavior |
|---|---|
| Relative `href` | Resolved to absolute via `base_url` |
| `mailto:` / `tel:` / `javascript:` / bare `#` links | Skipped |
| `<a>` with no `href` at all | Skipped, no crash |
| Duplicate links | Deduplicated, first occurrence order kept |
| Non-HTML `Content-Type` | `UnsupportedContentTypeError`, no parse attempt |
| 4xx response | `FetchError`, not retried |
| 5xx response | Retried with backoff, then `FetchError` |
| Connection refused / unreachable host | `FetchError`, no hang |
| CSS selector matches nothing | Empty list, not an error |
| Ragged table rows (missing/extra cells) | Missing → `None`, extra → ignored |
| Table with no header row | Documented behavior: first row becomes headers |
| Unclosed/malformed HTML tags | Parsed leniently by `html.parser`, no crash |
| Empty page | Empty results, no crash |
| robots.txt disallows the path | `RobotsDisallowedError` before any fetch |
| robots.txt missing/unreachable | Fails open — fetch proceeds |

## Examples

```
$ ./web_scraper.py https://example.com --links
https://example.com/about
https://external.example.com/page

$ ./web_scraper.py https://example.com --selector "h1"
Main Heading

$ ./web_scraper.py https://example.com --table
[
  {"Name": "Ada", "Role": "Engineer", "Years": "10"},
  {"Name": "Grace", "Role": "Engineer", "Years": null}
]
```

## Limitations

- No JavaScript execution — pages that render content client-side won't
  have that content in the fetched HTML. A headless-browser backend
  (Playwright/Selenium) would be needed for those.
- `RobotsChecker` caches one parser per origin for the process lifetime;
  a long-running scrape won't pick up a robots.txt change mid-run.
- No pagination-following logic — the caller extracts links and decides
  what to fetch next; this project doesn't implement crawl traversal.

## Lessons Learned

The malformed-HTML test initially asserted that extracting `h1` text from
a page with an unclosed `<h1>` would return just the heading text. It
didn't — `html.parser`'s leniency means an unclosed tag absorbs everything
that follows as nested content until the next tag that implicitly closes
it, so the "heading" text included the entire rest of the body. That's not
a bug in `extract_text()`; it's real, documented parser behavior. The test
was wrong, not the code — fixed the assertion to check for a crash-free
result with the expected prefix rather than an exact match, and left a
comment explaining why, so the next person reading the test doesn't
"fix" it back to the wrong assumption.

## Future Improvements

- Optional `lxml` backend for large-scale scraping.
- Pagination/crawl-following helper (breadth-limited link traversal).
- Structured extraction via a declarative schema (map of field → selector)
  instead of calling `extract_text`/`extract_table` separately per field.

## References

- BeautifulSoup documentation
- robots.txt / Robots Exclusion Protocol (RFC 9309)
- `requests` library documentation
