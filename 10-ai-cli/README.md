# 10 — AI CLI Assistant

A defensive command-line assistant backed by the Anthropic Messages API,
with prompt templating and persistent multi-turn conversation history.
Follows the same defensive-HTTP-client design as the REST API client
project (02) — typed exceptions, retry with backoff for transient
failures, no retry for failures that won't fix themselves — applied
specifically to an LLM backend.

## Problem

A CLI wrapper around an LLM API (`requests.post(url, json=payload).json()["content"][0]["text"]`)
breaks in the same ways any naive HTTP client does — plus a few LLM-
specific ones: a corrupted local history file shouldn't block every
future run, a prompt template with a missing variable shouldn't produce
a raw `KeyError`, and an unbounded conversation history shouldn't make
every subsequent request larger and more expensive forever.

## Requirements

- Send a message to the Anthropic Messages API and print the reply.
- Support a system prompt and configurable model/max-tokens.
- Persist multi-turn conversation history to a local JSON file between
  CLI invocations (each `chat` call is a separate process — history has
  to survive on disk, not just in memory).
- Bound history growth so a long-running session doesn't make every
  request larger and slower forever.
- Support prompt templates with named variables, failing clearly (not
  with a raw `KeyError`) when a variable is missing.
- Retry transient API failures (429, 5xx, connection errors, timeouts)
  with backoff; never retry failures that won't resolve themselves (bad
  API key, malformed request).
- Recover from a corrupted local history file by starting fresh rather
  than refusing to run.
- Be fully testable without live API access, since neither network
  egress nor the `anthropic` SDK is available in this environment.

## Architecture

```
ai_cli.py
├── PromptTemplate         # string.Template wrapper, clear missing-var errors
├── Conversation            # history load/save/trim, JSON-backed
├── LLMBackend (protocol)   # send(messages, system, model, max_tokens) -> LLMReply
├── AnthropicBackend        # real implementation: requests + retry/backoff
└── run_chat()               # orchestrates: load history → call backend → save history
```

`LLMBackend` is a small protocol class with one method. `AnthropicBackend`
is the real implementation; the test suite uses a `FakeBackend` that
implements the same interface. This is the same shape as `CommandRunner`
in the SSH automation project (07) and `move_fn` in the file organizer
(05) — the portfolio's recurring pattern for testing code whose real
dependency (network, filesystem permissions, an SSH server) isn't
available or controllable in the test environment.

## Design Decisions

- **`LLMBackend` as an explicit, minimal protocol, not a direct
  dependency on the `anthropic` SDK.** The SDK isn't installed in this
  environment (no network egress to install it), so this project talks
  to the API directly via `requests`, matching the raw HTTP shape
  documented for the Messages API. The protocol boundary also means
  swapping in the real SDK later — or a different model provider
  entirely — would only require a new class implementing `send()`, not
  changes to `Conversation`, `PromptTemplate`, or `run_chat()`.
- **Conversation history is trimmed to the most recent
  `MAX_HISTORY_MESSAGES`, not summarized.** Summarizing old history to
  preserve context would be a meaningfully bigger feature (it needs its
  own LLM call, its own failure handling, its own quality tradeoffs) —
  simple recency-based trimming is the honest, scoped-appropriately
  solution here, with summarization noted as a real future direction
  rather than implemented half-way.
- **A corrupted history file raises a specific, catchable
  `CorruptedHistoryError`, and the CLI layer chooses to treat it as
  recoverable** (the error is caught in `main()`, not in `Conversation.
  load()`) by telling the user how to fix it, while `Conversation.load()`
  itself stays strict and honest about the file being unreadable. Keeping
  the "is this recoverable" decision at the CLI layer, not buried inside
  the loading function, means a caller using this as a library (not the
  CLI) gets to make that call itself instead of having it made for them.
- **Prompt template variable errors are re-raised as
  `TemplateRenderError` naming the specific missing variable**, not left
  as a raw `KeyError` from `string.Template`. A CLI user hitting a
  missing-variable error needs to know *which* variable, immediately —
  not need to read a stack trace to find out.
- **Empty user input is rejected before any network call**, in
  `run_chat()`, not left to the API to reject. Failing fast, locally,
  for input that's obviously invalid avoids spending a network round
  trip (and, for a real API, cost) on a request that was never going to
  succeed.

## Algorithms Used

- Exponential backoff for retryable HTTP failures — same
  `min(base * 2^attempt, cap)` pattern as the API client (02), web
  scraper (03), and SSH toolkit (07) projects, with `Retry-After` header
  support for 429s.
- Recency-based sliding-window history trimming.

## Tradeoffs

- History trimming is unconditional recency-based truncation, not
  summarization — simpler and has no failure modes of its own, but a
  very long conversation loses its earliest context entirely rather than
  a compressed version of it.
- No streaming response support — the CLI waits for the full response
  before printing anything. Streaming would improve perceived latency
  for long replies but adds real complexity (partial-response error
  handling, a different backend interface shape) that wasn't justified
  for a portfolio project demonstrating the *defensive-client* pattern
  specifically.
- `AnthropicBackend` talks to the API via raw `requests` calls rather
  than the official SDK, since the SDK wasn't installable here. A real
  deployment would likely prefer the SDK for its more complete type
  definitions and built-in retry handling — this project's own retry
  logic exists specifically because the SDK wasn't an option.

## Edge Cases Handled

| Case | Behavior |
|---|---|
| No API key set | `MissingAPIKeyError`, clear message, before any request |
| Empty message | Rejected locally, no network call made |
| Missing template variable | `TemplateRenderError` naming the specific variable |
| History file doesn't exist yet | Treated as an empty conversation (normal first run) |
| History file is corrupted/wrong shape | `CorruptedHistoryError`, with a hint to delete and restart |
| Conversation grows very long | Silently trimmed to the most recent N messages |
| API returns 429 | Retried, honoring `Retry-After` if present |
| API returns 5xx | Retried with backoff, then a clear error if exhausted |
| API returns 401/400 (other 4xx) | Raised immediately, never retried |
| API response isn't valid JSON | `LLMResponseFormatError`, not a raw parse crash |
| API response is JSON but missing expected fields | `LLMResponseFormatError`, not a raw `KeyError` |
| Response has multiple text content blocks | Concatenated into one reply, not just the first block |
| Connection refused / timeout | Retried, then a clear `LLMRequestError` if exhausted |

## Examples

```
$ export ANTHROPIC_API_KEY=sk-...
$ ./ai_cli.py chat "What's a good name for a CLI tool?" --history session.json
Here are a few options: ...

$ ./ai_cli.py chat "Make it shorter" --history session.json
# The second call includes the first turn's history automatically.

$ ./ai_cli.py new --history session.json
conversation history cleared: session.json
```

## Limitations

- Not tested against the live Anthropic API — no network egress or SDK
  was available in this environment. The retry/error-classification/
  templating/history logic is fully tested via an injected fake backend
  and a fake `requests.Session`; the literal request shape (payload
  fields, headers) has only been verified by inspection against the
  documented API format, not by an actual successful call.
- No streaming support.
- History trimming is unconditional truncation, not summarization.
- Single conversation per history file — no support for named,
  multiple concurrent conversation threads in one file.

## Lessons Learned

The first draft of `_parse_response` assumed `data["content"][0]["text"]`
directly — the single-block case. Writing
`test_multiple_text_blocks_concatenated` (prompted by checking the
documented API response shape, which allows multiple content blocks, not
just checking the happy-path single-block case I'd been assuming) caught
that this would silently drop any text beyond the first block for a
multi-block response. Fixed by iterating all blocks of type `"text"` and
concatenating, which is also what makes a genuinely malformed response
(no text blocks at all) correctly distinguishable from "the reply was
just empty" — both raise `LLMResponseFormatError`, but for the right
reason.

## Future Improvements

- Optional streaming response support.
- History summarization for very long conversations, instead of pure
  truncation.
- Named multi-conversation support within one history file.
- Official SDK backend as an alternative to the raw `requests`
  implementation, selectable at construction time.

## References

- Anthropic Messages API documentation (request/response shape)
- Python `string.Template` documentation
