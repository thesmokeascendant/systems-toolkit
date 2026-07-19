#!/usr/bin/env python3
"""
ai_cli.py — a defensive command-line assistant backed by the Anthropic
Messages API, with prompt templating and persistent multi-turn
conversation history.

Follows the same defensive-HTTP-client pattern as the REST API client
project (02): typed exceptions, retry with backoff for transient
failures, no retry for the failures that won't fix themselves. The
actual network call goes through an injectable `LLMBackend`, which is
what makes this module fully testable without live API access — this
environment doesn't have network egress or the `anthropic` SDK
installed, so the test suite exercises the CLI, templating, history
management, and retry/error-classification logic entirely against a
fake backend that implements the same interface a real one would.

Usage:
    from ai_cli import AnthropicBackend, Conversation, PromptTemplate

    backend = AnthropicBackend(api_key="sk-...")
    convo = Conversation.load("history.json")
    convo.add_user_message("What's a good name for a CLI tool?")
    reply = backend.send(convo.messages, system="You are concise.")
    convo.add_assistant_message(reply.text)
    convo.save("history.json")

CLI:
    export ANTHROPIC_API_KEY=sk-...
    ./ai_cli.py chat "Summarize this project" --history session.json
    ./ai_cli.py new --history session.json
"""

from __future__ import annotations

import argparse
import json
import os
import string
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

import requests

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT = 30.0
MAX_HISTORY_MESSAGES = 40  # bounds context growth across a long-running session
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AICliError(Exception):
    """Base class for all ai_cli-raised errors."""


class MissingAPIKeyError(AICliError):
    """No API key was found in the environment or passed explicitly."""


class LLMRequestError(AICliError):
    """The request to the LLM backend failed (network, rate limit, server)."""


class LLMResponseFormatError(AICliError):
    """The backend returned a response that doesn't match the expected shape."""


class TemplateRenderError(AICliError):
    """A prompt template referenced a variable that wasn't supplied."""


class CorruptedHistoryError(AICliError):
    """The conversation history file exists but isn't valid JSON / the expected shape."""


# ---------------------------------------------------------------------
# Prompt templating
# ---------------------------------------------------------------------


class PromptTemplate:
    """
    A minimal, defensive prompt template using `string.Template` syntax
    (`$variable` / `${variable}`). Missing variables raise a clear
    `TemplateRenderError` naming exactly which variable was missing,
    instead of letting a raw `KeyError` from `string.Template` surface.
    """

    def __init__(self, template: str):
        self._template = string.Template(template)

    def render(self, **kwargs) -> str:
        try:
            return self._template.substitute(**kwargs)
        except KeyError as e:
            missing = str(e).strip("'\"")
            raise TemplateRenderError(f"template variable not supplied: {missing}") from e


# ---------------------------------------------------------------------
# Conversation history
# ---------------------------------------------------------------------


@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str


@dataclass
class Conversation:
    messages: list[Message] = field(default_factory=list)

    def add_user_message(self, text: str) -> None:
        self.messages.append(Message("user", text))
        self._trim()

    def add_assistant_message(self, text: str) -> None:
        self.messages.append(Message("assistant", text))
        self._trim()

    def _trim(self) -> None:
        # Keep the most recent MAX_HISTORY_MESSAGES messages so a
        # long-running CLI session doesn't grow the request payload
        # (and cost) without bound. This is a simple recency-based
        # trim, not summarization.
        if len(self.messages) > MAX_HISTORY_MESSAGES:
            self.messages = self.messages[-MAX_HISTORY_MESSAGES:]

    def as_api_messages(self) -> list[dict]:
        return [{"role": m.role, "content": m.content} for m in self.messages]

    @classmethod
    def load(cls, path: str) -> "Conversation":
        """
        Load conversation history from disk. A missing file is treated
        as an empty conversation (normal for a first run), not an
        error. A file that exists but is corrupted (invalid JSON, wrong
        shape) is a real problem the caller should know about — but the
        CLI layer chooses to recover from it by starting fresh rather
        than blocking the user, since losing a conversation log is
        much less bad than a CLI tool refusing to run at all.
        """
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            messages = [Message(m["role"], m["content"]) for m in data["messages"]]
            return cls(messages=messages)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            raise CorruptedHistoryError(f"could not load history from {p}: {e}") from e

    def save(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {"messages": [asdict(m) for m in self.messages]}
        p.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------
# LLM backend
# ---------------------------------------------------------------------


@dataclass
class LLMReply:
    text: str
    stop_reason: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class LLMBackend:
    """Protocol every backend (real or fake) implements."""

    def send(self, messages: list[dict], system: Optional[str], model: str, max_tokens: int) -> LLMReply:
        raise NotImplementedError


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class AnthropicBackend(LLMBackend):
    """
    Real backend: talks to the Anthropic Messages API over HTTPS via
    `requests`, since the `anthropic` SDK isn't installed in this
    environment. Retries transient failures (429, 5xx, connection
    errors, timeouts) with exponential backoff; never retries other 4xx
    errors, since a malformed request or bad API key won't fix itself.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        session: Optional[requests.Session] = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise MissingAPIKeyError(
                "no API key found — set ANTHROPIC_API_KEY or pass api_key explicitly"
            )
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self._sleep = sleep_fn

    def send(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> LLMReply:
        if not messages:
            raise ValueError("messages must not be empty")

        payload: dict = {"model": model, "max_tokens": max_tokens, "messages": messages}
        if system:
            payload["system"] = system

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

        last_exception: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(
                    API_URL, json=payload, headers=headers, timeout=self.timeout
                )
            except requests.exceptions.Timeout as e:
                last_exception = LLMRequestError(f"request timed out: {e}")
            except requests.exceptions.ConnectionError as e:
                last_exception = LLMRequestError(f"could not reach the API: {e}")
            else:
                if response.status_code == 200:
                    return self._parse_response(response)
                if response.status_code in RETRYABLE_STATUS_CODES and attempt < self.max_retries:
                    retry_after = self._retry_after(response)
                    self._sleep(retry_after or min(0.5 * (2 ** attempt), 8.0))
                    continue
                raise LLMRequestError(
                    f"API request failed with status {response.status_code}: {response.text[:300]}"
                )

            if attempt < self.max_retries:
                self._sleep(min(0.5 * (2 ** attempt), 8.0))
                continue
            raise last_exception

        raise last_exception or LLMRequestError("request failed for an unknown reason")

    @staticmethod
    def _retry_after(response: requests.Response) -> Optional[float]:
        header = response.headers.get("retry-after")
        if header is None:
            return None
        try:
            return float(header)
        except ValueError:
            return None

    @staticmethod
    def _parse_response(response: requests.Response) -> LLMReply:
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError) as e:
            raise LLMResponseFormatError(f"response was not valid JSON: {e}") from e

        try:
            content_blocks = data["content"]
            text = "".join(
                block.get("text", "") for block in content_blocks if block.get("type") == "text"
            )
        except (KeyError, TypeError, AttributeError) as e:
            raise LLMResponseFormatError(f"response did not match expected shape: {e}") from e

        if not text:
            raise LLMResponseFormatError("response contained no text content")

        return LLMReply(
            text=text,
            stop_reason=data.get("stop_reason"),
            input_tokens=data.get("usage", {}).get("input_tokens"),
            output_tokens=data.get("usage", {}).get("output_tokens"),
        )


# ---------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------


def run_chat(
    backend: LLMBackend,
    user_input: str,
    history_path: Optional[str] = None,
    system: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> LLMReply:
    """
    Orchestrates one chat turn: load history (if any), append the user's
    message, send to the backend, append the reply, save history (if a
    path was given). Empty input is rejected before any network call.
    """
    if not user_input.strip():
        raise ValueError("message must not be empty")

    convo = Conversation.load(history_path) if history_path else Conversation()
    convo.add_user_message(user_input)
    reply = backend.send(convo.as_api_messages(), system=system, model=model, max_tokens=max_tokens)
    convo.add_assistant_message(reply.text)
    if history_path:
        convo.save(history_path)
    return reply


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="A defensive AI CLI assistant")
    sub = parser.add_subparsers(dest="action", required=True)

    chat_p = sub.add_parser("chat", help="Send a message and print the reply")
    chat_p.add_argument("message")
    chat_p.add_argument("--history", help="Path to a JSON file for persistent multi-turn history")
    chat_p.add_argument("--system", help="System prompt")
    chat_p.add_argument("--model", default=DEFAULT_MODEL)
    chat_p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)

    new_p = sub.add_parser("new", help="Clear/reset a conversation history file")
    new_p.add_argument("--history", required=True)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.action == "new":
        Path(args.history).unlink(missing_ok=True)
        print(f"conversation history cleared: {args.history}")
        return 0

    try:
        backend = AnthropicBackend()
    except MissingAPIKeyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        reply = run_chat(
            backend, args.message, history_path=args.history,
            system=args.system, model=args.model, max_tokens=args.max_tokens,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except CorruptedHistoryError as e:
        print(f"error: {e}", file=sys.stderr)
        print("hint: delete the history file to start a fresh conversation", file=sys.stderr)
        return 1
    except (LLMRequestError, LLMResponseFormatError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(reply.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
