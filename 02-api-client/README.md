# 02 — REST API Client

A defensive HTTP JSON client that retries transient failures with
exponential backoff, honors `Retry-After` on rate limiting, and never lets
a malformed response or an unreachable host turn into an unhandled
exception in caller code.

## Problem

Naive API client code (`requests.get(url).json()`) works right up until
the network hiccups, the server returns a transient 500, the API starts
rate-limiting you, or a response claims to be JSON but isn't. Production
code talking to a real API needs to treat all of that as the normal case.

## Requirements

- Retry connection errors, timeouts, 429, and 5xx with exponential backoff.
- Never retry other 4xx errors — a bad request doesn't fix itself.
- Honor a `Retry-After` header on 429 instead of guessing a delay.
- Distinguish failure modes with specific exception types so callers can
  handle each meaningfully instead of catching a generic `Exception`.
- Never crash on a malformed JSON body — raise a specific, catchable error
  with the raw body attached for debugging.
- Work correctly when the host is completely unreachable (offline mode).
- Be testable without depending on a live external API.

## Architecture

```
api_client.py
├── RetryPolicy           # backoff math, isolated and independently testable
├── APIClient
│   ├── get() / post()    # public surface
│   ├── _request()        # retry loop: connection/timeout handling
│   └── _handle_response()# status-code routing: retry, raise, or parse
└── Exception hierarchy   # APIError → {Connection, Timeout, Decode,
                           #             Client, Server, RateLimitExceeded}
```

`_request()` owns the retry loop and only knows about *transport* failures
(timeouts, connection errors). `_handle_response()` owns *HTTP-level*
decisions (which status codes are retryable, which are fatal). Keeping
those two concerns in separate methods is what makes each one testable and
readable in isolation.

## Design Decisions

- **A typed exception hierarchy, not one generic `APIError`.** A caller
  handling a 429 needs to know how long to back off; a caller handling a
  404 needs to know the resource doesn't exist. Collapsing those into one
  exception type would force every caller to inspect a status code anyway
  — so the type itself carries the meaning.
- **`sleep_fn` is injectable.** Retry logic that calls `time.sleep()`
  directly is untestable without your test suite actually waiting for real
  backoff delays. Injecting the sleep function lets tests verify retry
  *behavior* (how many attempts, in what order) in milliseconds instead of
  seconds.
- **Tests run against a real local `http.server`, not mocked internals.**
  Mocking `requests.get` verifies that your code called the mock the way
  you expected — it doesn't verify your code handles real HTTP semantics
  (headers, status lines, connection resets) correctly. A local server on
  an ephemeral port gives real integration coverage while staying fully
  offline and fast.
- **429 and 5xx share the same backoff mechanism** but 429 additionally
  checks `Retry-After` first, since the server is telling you exactly how
  long to wait rather than making you guess.

## Algorithms Used

- Exponential backoff: `min(base_delay * 2^attempt, max_delay)`.
- `Retry-After` header parsing with graceful fallback to the backoff
  schedule if the header is missing or malformed.

## Tradeoffs

- Backoff is a simple exponential curve with no jitter. For a client
  hammered by many concurrent callers, adding jitter would reduce
  thundering-herd retries — deferred as a future improvement since this
  client is designed for single-caller use.
- `_request()`'s control flow (continue/raise across two exception sources
  and a sentinel return value) is denser than ideal. It was written this
  way to keep the retry loop in exactly one place rather than duplicating
  it across the connection-error path and the status-code path. See the
  engineering journal for the alternative that was rejected.

## Edge Cases Handled

| Case | Behavior |
|---|---|
| Connection refused / host unreachable | `APIConnectionError`, no hang |
| Request exceeds timeout | Retried, then `APITimeoutError` if exhausted |
| HTTP 429 with `Retry-After` | Waits the specified time, retries |
| HTTP 429 without `Retry-After` | Falls back to exponential backoff |
| HTTP 500/502/503/504 | Retried with backoff, then `ServerError` |
| HTTP 400/401/403/404 (non-429 4xx) | Raised immediately, never retried |
| Malformed JSON body | `APIDecodeError` with raw body attached, no crash |
| Empty response body | Returns `None` instead of raising |
| Retries exhausted on any transient failure | Specific exception raised, not a silent empty result |

## Examples

```
$ ./api_client.py http://127.0.0.1:8080/users/1
{
  "id": 1,
  "name": "example"
}

$ ./api_client.py http://127.0.0.1:8080/rate-limited
error: rate limited, retry after 30.0s
```

## Limitations

- No connection pooling tuning exposed (uses `requests.Session` defaults).
- Retry policy is global per client instance, not configurable per-request.
- No circuit breaker — a persistently down host is retried on every call
  rather than being short-circuited after repeated failures.
- POST retries assume idempotency; the client doesn't distinguish
  idempotent from non-idempotent requests, so retrying a POST after a
  timeout could, for a non-idempotent endpoint, double-submit. A real
  production client would need idempotency keys or method-aware retry
  rules.

## Lessons Learned

The first draft called `time.sleep()` directly inside the retry loop,
which meant the retry tests actually slept for real — a handful of retry
tests turned a fast suite into a several-second one, and it discouraged
writing more of them. Making `sleep_fn` an injectable dependency (default
`time.sleep`, test-injected `lambda s: None`) fixed both problems: the
production code path is unchanged, but the test suite runs in
milliseconds and can assert on retry *counts* without waiting through
backoff delays.

## Future Improvements

- Jittered backoff for multi-caller scenarios.
- Circuit breaker for persistently unreachable hosts.
- Per-request retry/timeout overrides.
- Idempotency-aware retry rules for POST/PUT/PATCH.

## References

- `requests` library documentation
- RFC 6585 (HTTP 429 Too Many Requests) — `Retry-After` semantics
- RFC 7231 §7.1.3 (`Retry-After` header)
