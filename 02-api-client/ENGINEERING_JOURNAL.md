# Engineering Journal — REST API Client

## Problem

Needed an HTTP client that treats network failure, rate limiting, and
malformed responses as expected inputs rather than exceptional crashes —
and needed a way to test all of that without depending on a live external
API (which would make the test suite flaky, slow, and network-dependent).

## Initial Idea

First draft used `unittest.mock.patch("requests.get")` to simulate
different responses (a `MagicMock` with `.status_code` and `.json()` set
per test).

## Why It Failed

Mocking `requests.get` only tests that my code *calls* `requests.get` the
way I expect and handles whatever the mock returns. It doesn't exercise
real HTTP semantics — a mocked response object always has whatever
`.json()` I told it to have; it can't accidentally have a `Content-Type`
mismatch, a truncated body, or a real socket timeout. Two bugs slipped
through this way in early testing: the timeout-handling code was catching
the wrong exception class (`requests.Timeout` vs `requests.exceptions.
ReadTimeout` — they're not always the same depending on where the timeout
occurs), and mocks never surfaced it because a mock never actually times
out.

## Alternative Approaches Considered

1. **Keep mocking `requests`, add more granular mock exception types.**
   Rejected — treats the symptom, not the cause. Still not testing against
   real HTTP behavior.
2. **Use `responses` or `httpretty` (request-mocking libraries).** Better
   than raw mocks, but still intercepts at the library boundary rather
   than testing real socket/HTTP-parsing behavior, and adds a dependency
   for something the standard library already solves.
3. **Spin up a real `http.server.HTTPServer` on an ephemeral port in a
   background thread for the test class.** Chosen. Real sockets, real HTTP
   responses, real timeouts — and it's pure standard library, so no new
   dependency. `setUpClass`/`tearDownClass` keep the server's lifecycle
   scoped to the test class instead of per-test overhead.

## Benchmarking / Performance Observations

Full 10-test suite runs in ~0.5s including a real (short) timeout test and
a real connection-refused attempt. The `sleep_fn` injection is what keeps
the retry-heavy tests fast — without it, `test_retries_on_500_then_succeeds`
alone would take several seconds of real backoff sleep.

## Refactoring Decisions

Originally `_request()` handled both transport errors (timeouts,
connection errors) and HTTP status-code routing in one large method with
retry logic duplicated in both branches. Split status-code routing out
into `_handle_response()`, which returns either a parsed value or a
sentinel (`_RETRY_SENTINEL`) telling the caller to retry. This keeps there
being exactly one retry loop instead of two near-identical copies that
would inevitably drift out of sync.

## Final Implementation

Single retry loop in `_request()`, transport errors caught directly,
HTTP-level decisions delegated to `_handle_response()`. Typed exception
hierarchy so callers can catch precisely what they care about
(`RateLimitExceeded` vs `ServerError` vs `ClientError`) instead of string-
matching an error message.

## Engineering Tradeoffs

Chose a real local HTTP server for tests over faster-but-shallower mocks.
The tradeoff is a bit more test setup complexity (`ScriptedHandler`,
thread lifecycle) in exchange for tests that would have actually caught
the timeout-exception-class bug the mock-based approach missed.

## Possible Future Versions

- Add jitter to backoff for multi-caller fairness.
- Add a circuit breaker so a host that's been down for N consecutive
  failures gets short-circuited instead of retried every call.
- Support async (`httpx`/`aiohttp`) variant for concurrent callers.
