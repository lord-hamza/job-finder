import json
import os
from unittest import mock

import requests

from core import llm
from core.tests.base import AppTestCase

SCHEMA = {"type": "OBJECT", "properties": {"ok": {"type": "BOOLEAN"}}}


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self._text = text

    def json(self):
        if self._text is not None:
            raise ValueError("not json")
        return self._payload


def answer(text, finish="STOP"):
    return FakeResponse(payload={"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}]})


def error(status, message):
    return FakeResponse(status, {"error": {"code": status, "message": message}})


class GenerateJsonTests(AppTestCase):
    def post(self, *responses):
        patcher = mock.patch("core.llm.requests.post", side_effect=list(responses))
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_request_matches_the_documented_rest_format(self):
        post = self.post(answer('{"ok": true}'))
        self.assertEqual(llm.generate_json("hello", SCHEMA, system="be good"), {"ok": True})
        url = post.call_args.args[0]
        kwargs = post.call_args.kwargs
        self.assertEqual(
            url, "https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent"
        )
        self.assertEqual(kwargs["headers"]["x-goog-api-key"], "test-gemini-key")
        self.assertNotIn("test-gemini-key", url)
        self.assertEqual(kwargs["json"]["contents"][0]["parts"], [{"text": "hello"}])
        self.assertEqual(kwargs["json"]["systemInstruction"], {"parts": [{"text": "be good"}]})
        self.assertEqual(
            kwargs["json"]["generationConfig"], {"responseMimeType": "application/json", "responseSchema": SCHEMA}
        )

    def test_custom_model(self):
        post = self.post(answer('{"ok": true}'))
        with mock.patch.dict(os.environ, {"GEMINI_MODEL": "gemini-3.6-flash"}):
            llm.generate_json("hi", SCHEMA)
        self.assertIn("/models/gemini-3.6-flash:generateContent", post.call_args.args[0])

    def test_pdf_is_sent_inline(self):
        post = self.post(answer('{"ok": true}'))
        llm.generate_json("read", SCHEMA, pdf=b"%PDF")
        part = post.call_args.kwargs["json"]["contents"][0]["parts"][0]
        self.assertEqual(part, {"inlineData": {"mimeType": "application/pdf", "data": "JVBERg=="}})

    def test_missing_key(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": " "}):
            with self.assertRaisesMessage(llm.LLMError, "isn't set"):
                llm.generate_json("hi", SCHEMA)

    def test_thought_parts_ignored_and_code_fences_tolerated(self):
        parts = [{"text": "thinking", "thought": True}, {"text": '```json\n{"ok": true}\n```'}]
        self.post(FakeResponse(payload={"candidates": [{"content": {"parts": parts}}]}))
        self.assertEqual(llm.generate_json("hi", SCHEMA), {"ok": True})

    def test_retries_rate_limits_then_succeeds(self):
        post = self.post(error(429, "slow down"), error(503, "busy"), answer('{"ok": true}'))
        self.assertEqual(llm.generate_json("hi", SCHEMA), {"ok": True})
        self.assertEqual(post.call_count, 3)

    def test_gives_up_after_three_tries(self):
        post = self.post(*[error(429, "quota gone")] * 6)
        with self.assertRaisesMessage(llm.LLMError, "quota gone"):
            llm.generate_json("hi", SCHEMA)
        self.assertEqual(post.call_count, 6)

    def test_overloaded_model_moves_straight_to_the_backup(self):
        post = self.post(error(503, "high demand"), answer('{"ok": true}'))
        self.assertEqual(llm.generate_json("hi", SCHEMA), {"ok": True})
        urls = [call.args[0] for call in post.call_args_list]
        self.assertIn("/models/gemini-flash-latest:", urls[0])
        self.assertIn(f"/models/{llm.FALLBACK_MODEL}:", urls[1])
        llm.time.sleep.assert_not_called()

    def test_hanging_model_moves_straight_to_the_backup(self):
        post = self.post(requests.Timeout("x"), answer('{"ok": true}'))
        self.assertEqual(llm.generate_json("hi", SCHEMA), {"ok": True})
        self.assertLessEqual(post.call_args.kwargs["timeout"][1], 60)

    def test_someone_waiting_is_never_kept_for_long(self):
        post = self.post(requests.Timeout("x"), error(503, "high demand"))
        with self.assertRaisesMessage(llm.LLMError, "high demand"):
            llm.generate_json("hi", SCHEMA, patient=False)
        self.assertEqual(post.call_count, 2)
        llm.time.sleep.assert_not_called()

    def test_timeout_message_says_what_happened(self):
        self.post(requests.Timeout("x"), requests.Timeout("x"))
        with self.assertRaisesMessage(llm.LLMError, "overloaded"):
            llm.generate_json("hi", SCHEMA, patient=False)

    def test_backup_tried_once_if_it_is_also_the_chosen_model(self):
        post = self.post(error(503, "busy"))
        with mock.patch.dict(os.environ, {"GEMINI_MODEL": llm.FALLBACK_MODEL}):
            with self.assertRaises(llm.LLMError):
                llm.generate_json("hi", SCHEMA, patient=False)
        self.assertEqual(post.call_count, 1)

    def test_default_model_is_the_cheap_fast_one(self):
        with mock.patch.dict(os.environ, {"GEMINI_MODEL": ""}):
            self.assertEqual(llm.model(), "gemini-flash-lite-latest")

    def test_network_errors_retried(self):
        post = self.post(requests.ConnectionError("x"), requests.Timeout("x"), answer('{"ok": true}'))
        self.assertEqual(llm.generate_json("hi", SCHEMA), {"ok": True})
        self.assertEqual(post.call_count, 3)

    def test_network_error_message_is_readable(self):
        self.post(*[requests.ConnectionError("x")] * 6)
        with self.assertRaisesMessage(llm.LLMError, "internet connection"):
            llm.generate_json("hi", SCHEMA)

    def test_invalid_json_retried(self):
        post = self.post(answer("not json"), answer('{"ok": true}'))
        self.assertEqual(llm.generate_json("hi", SCHEMA), {"ok": True})
        self.assertEqual(post.call_count, 2)

    def test_json_that_is_not_an_object_retried(self):
        post = self.post(answer("[1, 2]"), answer('{"ok": true}'))
        self.assertEqual(llm.generate_json("hi", SCHEMA), {"ok": True})
        self.assertEqual(post.call_count, 2)

    def test_unreadable_http_body_retried(self):
        post = self.post(FakeResponse(text="<html>"), answer('{"ok": true}'))
        self.assertEqual(llm.generate_json("hi", SCHEMA), {"ok": True})
        self.assertEqual(post.call_count, 2)

    def test_bad_key_not_retried(self):
        post = self.post(error(400, "API key not valid. Please pass a valid API key."))
        with self.assertRaisesMessage(llm.LLMError, "rejected the API key"):
            llm.generate_json("hi", SCHEMA)
        self.assertEqual(post.call_count, 1)

    def test_forbidden_not_retried(self):
        post = self.post(error(403, "Permission denied"))
        with self.assertRaisesMessage(llm.LLMError, "rejected the API key"):
            llm.generate_json("hi", SCHEMA)
        self.assertEqual(post.call_count, 1)

    def test_retired_model_says_what_to_change(self):
        self.post(error(404, "not found"))
        with self.assertRaisesMessage(llm.LLMError, "GEMINI_MODEL"):
            llm.generate_json("hi", SCHEMA)

    def test_error_body_wrapped_in_a_list(self):
        self.post(FakeResponse(403, [{"error": {"message": "denied"}}]))
        with self.assertRaisesMessage(llm.LLMError, "denied"):
            llm.generate_json("hi", SCHEMA)

    def test_non_json_error_body(self):
        self.post(FakeResponse(400, text="<html>"))
        with self.assertRaisesMessage(llm.LLMError, "HTTP 400"):
            llm.generate_json("hi", SCHEMA)

    def test_blocked_prompt_not_retried(self):
        post = self.post(FakeResponse(payload={"promptFeedback": {"blockReason": "SAFETY"}}))
        with self.assertRaisesMessage(llm.LLMError, "SAFETY"):
            llm.generate_json("hi", SCHEMA)
        self.assertEqual(post.call_count, 1)

    def test_safety_stop_not_retried(self):
        post = self.post(FakeResponse(payload={"candidates": [{"finishReason": "SAFETY"}]}))
        with self.assertRaisesMessage(llm.LLMError, "SAFETY"):
            llm.generate_json("hi", SCHEMA)
        self.assertEqual(post.call_count, 1)

    def test_no_candidates_or_truncated_answer_retried(self):
        truncated = json.dumps({"ok": True})[:5]
        post = self.post(FakeResponse(payload={}), answer(truncated, "MAX_TOKENS"), answer('{"ok": true}'))
        self.assertEqual(llm.generate_json("hi", SCHEMA), {"ok": True})
        self.assertEqual(post.call_count, 3)


class VerifyKeyTests(AppTestCase):
    def test_good_key(self):
        with mock.patch("core.llm.requests.get", return_value=FakeResponse(payload={"models": []})) as get:
            llm.verify_key("new-key")
        self.assertEqual(get.call_args.kwargs["headers"]["x-goog-api-key"], "new-key")

    def test_bad_key(self):
        with mock.patch("core.llm.requests.get", return_value=error(400, "API key not valid")):
            with self.assertRaisesMessage(llm.LLMError, "rejected the API key"):
                llm.verify_key("bad")

    def test_offline(self):
        with mock.patch("core.llm.requests.get", side_effect=requests.ConnectionError("x")):
            with self.assertRaisesMessage(llm.LLMError, "internet connection"):
                llm.verify_key("key")
