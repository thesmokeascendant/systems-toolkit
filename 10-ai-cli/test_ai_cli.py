"""
Unit tests for ai_cli.py

No live network or `anthropic` SDK is available in this environment, so
AnthropicBackend is tested by injecting a fake `requests.Session`-shaped
object (same dependency-injection pattern as the API client project,
02). Conversation/PromptTemplate/run_chat are tested against a small
in-memory FakeBackend implementing the same LLMBackend interface a real
backend would.

Run with:
    python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai_cli import (  # noqa: E402
    AnthropicBackend,
    Conversation,
    CorruptedHistoryError,
    LLMBackend,
    LLMReply,
    LLMRequestError,
    LLMResponseFormatError,
    MissingAPIKeyError,
    PromptTemplate,
    TemplateRenderError,
    run_chat,
)


class FakeBackend(LLMBackend):
    """Records every call it receives and returns scripted replies in order."""

    def __init__(self, replies: list[str]):
        self.replies = replies
        self.calls: list[dict] = []

    def send(self, messages, system, model, max_tokens):
        self.calls.append(
            {"messages": messages, "system": system, "model": model, "max_tokens": max_tokens}
        )
        return LLMReply(text=self.replies[len(self.calls) - 1])


class FakeResponse:
    def __init__(self, status_code, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class FakeSession:
    """Scripted stand-in for requests.Session, consumed step by step."""

    def __init__(self, script: list[FakeResponse]):
        self.script = script
        self.calls = 0

    def post(self, url, json, headers, timeout):
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        return step


class TestPromptTemplate(unittest.TestCase):
    def test_renders_with_all_variables_supplied(self):
        t = PromptTemplate("Translate to $language: $text")
        result = t.render(language="French", text="hello")
        self.assertEqual(result, "Translate to French: hello")

    def test_missing_variable_raises_clear_error(self):
        t = PromptTemplate("Translate to $language: $text")
        with self.assertRaises(TemplateRenderError) as ctx:
            t.render(language="French")
        self.assertIn("text", str(ctx.exception))

    def test_extra_unused_variables_ignored(self):
        t = PromptTemplate("Hello $name")
        result = t.render(name="Ada", unused="ignored")
        self.assertEqual(result, "Hello Ada")


class TestConversation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_missing_file_returns_empty_conversation(self):
        convo = Conversation.load(str(self.root / "does_not_exist.json"))
        self.assertEqual(convo.messages, [])

    def test_save_and_load_round_trip(self):
        convo = Conversation()
        convo.add_user_message("hello")
        convo.add_assistant_message("hi there")
        path = str(self.root / "history.json")
        convo.save(path)

        loaded = Conversation.load(path)
        self.assertEqual(len(loaded.messages), 2)
        self.assertEqual(loaded.messages[0].role, "user")
        self.assertEqual(loaded.messages[1].content, "hi there")

    def test_corrupted_json_raises_clear_error(self):
        path = self.root / "bad.json"
        path.write_text("{not valid json at all")
        with self.assertRaises(CorruptedHistoryError):
            Conversation.load(str(path))

    def test_wrong_shape_json_raises_clear_error(self):
        path = self.root / "wrong_shape.json"
        path.write_text('{"not_messages": []}')
        with self.assertRaises(CorruptedHistoryError):
            Conversation.load(str(path))

    def test_history_trimmed_beyond_max(self):
        convo = Conversation()
        for i in range(100):
            convo.add_user_message(f"message {i}")
        self.assertLessEqual(len(convo.messages), 40)
        # Most recent message survives the trim.
        self.assertEqual(convo.messages[-1].content, "message 99")

    def test_as_api_messages_shape(self):
        convo = Conversation()
        convo.add_user_message("hi")
        api_messages = convo.as_api_messages()
        self.assertEqual(api_messages, [{"role": "user", "content": "hi"}])


class TestRunChat(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_empty_message_rejected_before_any_backend_call(self):
        backend = FakeBackend(["should not be used"])
        with self.assertRaises(ValueError):
            run_chat(backend, "   ")
        self.assertEqual(len(backend.calls), 0)

    def test_reply_returned_and_history_persisted(self):
        backend = FakeBackend(["Hello there!"])
        path = str(self.root / "session.json")
        reply = run_chat(backend, "Hi", history_path=path)
        self.assertEqual(reply.text, "Hello there!")

        convo = Conversation.load(path)
        self.assertEqual(len(convo.messages), 2)
        self.assertEqual(convo.messages[0].content, "Hi")
        self.assertEqual(convo.messages[1].content, "Hello there!")

    def test_second_turn_includes_prior_history_in_the_request(self):
        backend = FakeBackend(["first reply", "second reply"])
        path = str(self.root / "session.json")
        run_chat(backend, "first message", history_path=path)
        run_chat(backend, "second message", history_path=path)

        second_call_messages = backend.calls[1]["messages"]
        contents = [m["content"] for m in second_call_messages]
        self.assertEqual(
            contents, ["first message", "first reply", "second message"]
        )

    def test_no_history_path_means_no_file_written(self):
        backend = FakeBackend(["ephemeral reply"])
        reply = run_chat(backend, "hi", history_path=None)
        self.assertEqual(reply.text, "ephemeral reply")
        # Nothing to assert on disk — just confirming this doesn't raise
        # trying to write to a None path.


class TestAnthropicBackend(unittest.TestCase):
    def test_missing_api_key_raises_before_any_request(self):
        import os

        old = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            with self.assertRaises(MissingAPIKeyError):
                AnthropicBackend(api_key=None)
        finally:
            if old is not None:
                os.environ["ANTHROPIC_API_KEY"] = old

    def test_successful_response_parsed_correctly(self):
        session = FakeSession([
            FakeResponse(200, {
                "content": [{"type": "text", "text": "Hello!"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 3},
            })
        ])
        backend = AnthropicBackend(api_key="k", session=session, sleep_fn=lambda s: None)
        reply = backend.send([{"role": "user", "content": "hi"}], None, "m", 50)
        self.assertEqual(reply.text, "Hello!")
        self.assertEqual(reply.input_tokens, 10)

    def test_multiple_text_blocks_concatenated(self):
        session = FakeSession([
            FakeResponse(200, {"content": [
                {"type": "text", "text": "Part one. "},
                {"type": "text", "text": "Part two."},
            ]})
        ])
        backend = AnthropicBackend(api_key="k", session=session, sleep_fn=lambda s: None)
        reply = backend.send([{"role": "user", "content": "hi"}], None, "m", 50)
        self.assertEqual(reply.text, "Part one. Part two.")

    def test_429_retried_then_succeeds(self):
        session = FakeSession([
            FakeResponse(429, text="rate limited", headers={"retry-after": "0"}),
            FakeResponse(200, {"content": [{"type": "text", "text": "recovered"}]}),
        ])
        backend = AnthropicBackend(api_key="k", session=session, sleep_fn=lambda s: None)
        reply = backend.send([{"role": "user", "content": "hi"}], None, "m", 50)
        self.assertEqual(reply.text, "recovered")
        self.assertEqual(session.calls, 2)

    def test_5xx_retried_then_exhausted_raises(self):
        session = FakeSession([FakeResponse(503, text="unavailable")])
        backend = AnthropicBackend(api_key="k", session=session, max_retries=2, sleep_fn=lambda s: None)
        with self.assertRaises(LLMRequestError):
            backend.send([{"role": "user", "content": "hi"}], None, "m", 50)
        self.assertEqual(session.calls, 3)  # initial + 2 retries

    def test_401_never_retried(self):
        session = FakeSession([FakeResponse(401, text="invalid api key")])
        backend = AnthropicBackend(api_key="bad", session=session, max_retries=3, sleep_fn=lambda s: None)
        with self.assertRaises(LLMRequestError):
            backend.send([{"role": "user", "content": "hi"}], None, "m", 50)
        self.assertEqual(session.calls, 1)  # not retried

    def test_malformed_json_response_raises_format_error(self):
        session = FakeSession([FakeResponse(200, json_data=None, text="not json")])
        backend = AnthropicBackend(api_key="k", session=session, sleep_fn=lambda s: None)
        with self.assertRaises(LLMResponseFormatError):
            backend.send([{"role": "user", "content": "hi"}], None, "m", 50)

    def test_response_missing_content_key_raises_format_error(self):
        session = FakeSession([FakeResponse(200, {"unexpected": "shape"})])
        backend = AnthropicBackend(api_key="k", session=session, sleep_fn=lambda s: None)
        with self.assertRaises(LLMResponseFormatError):
            backend.send([{"role": "user", "content": "hi"}], None, "m", 50)

    def test_empty_messages_rejected(self):
        session = FakeSession([FakeResponse(200, {})])
        backend = AnthropicBackend(api_key="k", session=session, sleep_fn=lambda s: None)
        with self.assertRaises(ValueError):
            backend.send([], None, "m", 50)


if __name__ == "__main__":
    unittest.main()
