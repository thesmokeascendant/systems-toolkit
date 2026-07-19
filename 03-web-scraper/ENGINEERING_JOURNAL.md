# Engineering Journal — Web Scraper

## Problem

Needed a scraper that's honest about the two very different kinds of
failure it faces: network/fetch failures (timeouts, bad status codes,
wrong content type) and parsing failures (malformed HTML, missing
elements, ragged tables). Conflating them in one function makes both
harder to test and harder to reason about.

## Initial Idea

First draft had a single `scrape(url, selector)` function that fetched
and parsed in one step, returning extracted text directly.

## Why It Failed

Testing it meant every parsing test also had to go over the network (or
mock `requests` deeply enough to fake a full response). Table-parsing
edge cases especially — ragged rows, missing headers — are pure data
transformations that have nothing to do with HTTP, but the combined
function forced every test through the fetch path anyway. It also meant
a single function had two very different exception vocabularies mixed
together (network errors and parsing errors), which made call-site error
handling awkward — you couldn't catch "the page doesn't have this
selector" separately from "the page couldn't be reached" because both
came out of the same function.

## Alternative Approaches Considered

1. **Keep the combined function, add a `dry_run=True` flag that skips the
   fetch for testing.** Rejected — a test-only code path in production
   code is a smell, and it still couples the two concerns at the API
   level even if tests can route around it.
2. **Split fetch and parse into separate functions returning/taking a
   `Page` object.** Chosen. `fetch_page()` returns a `Page` (url, status,
   html, parsed soup); `extract_links`/`extract_text`/`extract_table` take
   a `BeautifulSoup` object directly and know nothing about HTTP. Parsing
   tests now load fixture HTML files straight into `BeautifulSoup` with no
   network involved at all.

## Benchmarking / Performance Observations

Not benchmarked for large-scale crawling — this project focuses on
correctness of a single fetch+parse cycle. Bulk/concurrent crawling would
need a different architecture (worker pool, shared rate limiter state
across workers) that's out of scope here.

## Refactoring Decisions

Pulled `PoliteRateLimiter` and `RobotsChecker` out as their own classes
with injectable `sleep_fn`/`clock` (rate limiter) and injectable `session`
(robots checker), mirroring the `sleep_fn` injection pattern from the API
client project. That's what let `test_waits_between_requests_to_same_host`
assert on exact computed delay values using a scripted fake clock instead
of a real one.

## Final Implementation

Fetch layer (`fetch_page`) handles retries, status codes, and content-type
gating, then hands off a `Page` to parsing functions that are pure and
side-effect-free. Rate limiting and robots-checking are optional
collaborators passed into `fetch_page`, not baked into it — a caller doing
one-off testing doesn't have to construct either.

## Engineering Tradeoffs

Choosing `html.parser` over `lxml` avoids a native-dependency requirement
for the portfolio but costs some parsing leniency surprises (see README
"Lessons Learned" — the unclosed-`<h1>`-swallows-everything behavior).
Decided that's an acceptable tradeoff for a project meant to demonstrate
defensive-parsing *technique*, not to be a maximally fast production
crawler.

## Possible Future Versions

- Swap in `lxml` as an optional faster backend.
- Add a small crawl-frontier helper that uses `extract_links` +
  `PoliteRateLimiter` together to walk a site breadth-first up to a depth
  limit.
- Structured field-mapping extraction (schema → dict) built on top of the
  existing `extract_text`/`extract_table` primitives.
