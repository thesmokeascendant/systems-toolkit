# Engineering Journal — AI CLI Assistant

## Problem

Needed a CLI assistant where a corrupted local history file, a missing
prompt-template variable, and a rate-limited or flaky API call are all
handled explicitly and recoverably — and where the whole thing is
testable despite this environment having neither network egress nor the
`anthropic` SDK installed.

## Initial Idea

First draft called the API directly inside the CLI's `main()` function
using a module-level `requests.post()` call, and parsed the response
with `data["content"][0]["text"]`.

## Why It Failed

Two separate problems, both found before writing any tests, by first
checking the documented Messages API response shape rather than assuming
the single-block case:

1. **Untestable.** With the request logic inlined into `main()`, there
   was no way to test retry/error-handling behavior without either a
   live API key (not available) or extensive mocking of `requests`
   scattered across `main()`. Every prior project in this portfolio that
   hit "the real dependency isn't available in this environment" solved
   it by extracting an injectable interface — this needed the same
   treatment.
2. **Response parsing assumed one content block.** The Messages API
   response `content` field is a *list* of content blocks — the
   documented format allows more than one text block in a single
   response. `data["content"][0]["text"]` would silently drop everything
   after the first block for a multi-block reply, with no error at all —
   the worst kind of bug, since it wouldn't even surface as a crash.

## Alternative Approaches Considered

For testability:
1. **Mock `requests.post` globally with `unittest.mock.patch` in every
   test.** Works, but patches a specific import path
   (`ai_cli.requests.post`), which is more brittle to refactors than an
   explicit parameter, and doesn't give a clean seam for a *fake*
   backend (as opposed to a fake HTTP layer) for the higher-level
   `Conversation`/`run_chat` tests.
2. **Extract an `LLMBackend` protocol with one method (`send`), and make
   `AnthropicBackend` an injectable implementation of it — the same
   dependency-injection pattern used for `CommandRunner` (SSH toolkit,
   07) and `move_fn` (file organizer, 05).** Chosen. This gives two
   testing seams: a `FakeBackend` implementing the full protocol for
   testing `Conversation`/`run_chat`/templating, and a `FakeSession`
   (matching `requests.Session`'s shape) for testing `AnthropicBackend`'s
   own retry and response-parsing logic specifically.

For response parsing:
1. **Keep `content[0]["text"]`, document the single-block assumption as
   a known limitation.** Rejected — this isn't a scope limitation, it's
   a correctness bug against the API's own documented response shape;
   documenting it instead of fixing it would be the wrong call.
2. **Iterate all content blocks, concatenate every block of type
   `"text"`.** Chosen, plus a `test_multiple_text_blocks_concatenated`
   test written specifically to pin this down and prevent a regression
   back to the single-block assumption.

## Benchmarking / Performance Observations

Not applicable in the traditional sense — no live API to benchmark
against. The retry/backoff logic is architecturally identical to the
REST API client project (02), which is where this portfolio's
retry-timing testing pattern (`sleep_fn` injection to keep tests fast)
originates.

## Refactoring Decisions

Split the "is this recoverable" decision for a corrupted history file
away from `Conversation.load()` itself. `Conversation.load()` raises
`CorruptedHistoryError` unconditionally when the file can't be parsed —
it doesn't get to decide for every caller that starting fresh is the
right recovery. `main()` (the CLI layer) is where that specific policy
choice — "for an interactive CLI tool, losing history is better than
refusing to run" — actually belongs, since a library caller using
`Conversation` directly might have a different opinion about whether
silent data loss is acceptable.

## Final Implementation

`LLMBackend` is a one-method protocol. `AnthropicBackend` implements it
via `requests`, with the same retry-classification shape as project 02's
`APIClient` (retryable: 429/5xx/timeout/connection error; not retryable:
other 4xx). `Conversation` handles JSON-backed persistence and recency-
based trimming. `PromptTemplate` wraps `string.Template` with a named-
variable error instead of a raw `KeyError`. `run_chat()` is the one
function that ties history loading, backend invocation, and history
saving together, and is what both the CLI's `main()` and any future
library caller would use.

## Engineering Tradeoffs

Chose raw `requests` calls over the official `anthropic` SDK purely
because the SDK wasn't installable in this environment (no network
egress for `pip install`). This is a real limitation worth being
explicit about: the SDK provides more complete type definitions,
built-in retry handling, and streaming support that this project
reimplements a scoped-down version of. The `LLMBackend` protocol
boundary is specifically what keeps this an implementation detail rather
than a design commitment — a `SDKBackend` class could be added later
without touching anything else.

## Possible Future Versions

- `SDKBackend` implementing `LLMBackend` via the official `anthropic`
  SDK, selectable alongside `AnthropicBackend`.
- Streaming response support (would need `LLMBackend.send` to become a
  generator-returning method, or a separate `send_streaming` method).
- History summarization instead of pure recency-based truncation for
  very long conversations.
