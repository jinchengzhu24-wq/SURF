import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIR = Path(__file__).resolve().parents[1]

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import llm_client


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)

        if isinstance(outcome, Exception):
            raise outcome

        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=outcome))]
        )


class FakeClient:
    def __init__(self, outcomes):
        self.chat = SimpleNamespace(completions=FakeCompletions(outcomes))


class LLMClientTests(unittest.TestCase):
    def execute(self, outcomes):
        client = FakeClient(outcomes)

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_get_client", return_value=client),
            patch.object(llm_client.time, "sleep"),
        ):
            result = llm_client.generate_chat_reply(
                [{"role": "user", "content": "Assess the level."}],
                ["############"] * 10,
                "request-test",
            )

        return result, client

    def test_valid_response_is_returned(self):
        result, client = self.execute(
            ['{"assistantMessage":"The level has a compact central route."}']
        )

        self.assertEqual(result.assistant_message, "The level has a compact central route.")
        self.assertEqual(result.attempts_used, 1)
        request = client.chat.completions.calls[0]
        self.assertEqual(request["response_format"], {"type": "json_object"})
        self.assertFalse(request["stream"])

    def test_invalid_json_is_retried_once(self):
        result, client = self.execute(
            [
                "not-json",
                '{"assistantMessage":"Please tell me what experience you want."}',
            ]
        )

        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(len(client.chat.completions.calls), 2)

    def test_two_invalid_responses_raise_safe_error(self):
        client = FakeClient(["not-json", json.dumps({"wrong": "field"})])

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(llm_client, "_get_client", return_value=client),
            patch.object(llm_client.time, "sleep"),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Hello."}],
                ["############"] * 10,
                "invalid-test",
            )

        self.assertEqual(raised.exception.code, "MODEL_RESPONSE_INVALID")
        self.assertEqual(raised.exception.attempts_used, 2)
        self.assertNotIn("wrong", raised.exception.safe_message)

    def test_missing_api_key_fails_without_model_call(self):
        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}),
            self.assertRaises(llm_client.LLMServiceError) as raised,
        ):
            llm_client.generate_chat_reply(
                [{"role": "user", "content": "Hello."}],
                ["############"] * 10,
                "config-test",
            )

        self.assertEqual(raised.exception.code, "CONFIGURATION_ERROR")
        self.assertEqual(raised.exception.attempts_used, 0)

    def test_non_ascii_response_is_rejected(self):
        with self.assertRaises(ValueError):
            llm_client.validate_chat_response({"assistantMessage": "Hello \u4e16\u754c"})


if __name__ == "__main__":
    unittest.main()

