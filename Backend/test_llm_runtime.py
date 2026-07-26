import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

import Backend.app as backend
import Backend.llm_runtime as runtime


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
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=outcome),
                )
            ],
            usage=None,
        )


class FakeClient:
    def __init__(self, outcomes):
        self.completions = FakeCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)


class LLMRuntimeTests(unittest.TestCase):
    def execute(self, client, **overrides):
        arguments = {
            "task": "test_task",
            "messages": [{"role": "user", "content": "Return JSON."}],
            "validator": lambda payload: payload,
            "temperature": 0.5,
            "timeout_seconds": 25,
            "max_attempts": 2,
            "request_id": "request-test",
            "validation_stage": "test_validation",
        }
        arguments.update(overrides)

        with (
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"}),
            patch.object(runtime, "_get_client", return_value=client),
            patch.object(runtime.time, "sleep"),
        ):
            return runtime.execute_json_request(**arguments)

    def test_invalid_json_is_repaired_once(self):
        client = FakeClient(["not-json", '{"value": 7}'])
        result = self.execute(client)

        self.assertEqual(result.value, {"value": 7})
        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(len(client.completions.calls), 2)
        repair_message = client.completions.calls[1]["messages"][-1]["content"]
        self.assertIn("failed validation", repair_message)
        self.assertIn("valid JSON", repair_message)

    def test_validation_error_is_included_in_repair_request(self):
        client = FakeClient(['{"value": 18}', '{"value": 12}'])

        def validate(payload):
            if payload["value"] > 16:
                raise ValueError("minPushes=18 is outside 8-16")

            return payload

        result = self.execute(client, validator=validate)

        self.assertEqual(result.value["value"], 12)
        self.assertEqual(result.attempts_used, 2)
        repair_message = client.completions.calls[1]["messages"][-1]["content"]
        self.assertIn("minPushes=18 is outside 8-16", repair_message)

    def test_model_budget_never_exceeds_requested_single_attempt(self):
        client = FakeClient(["not-json", '{"value": 7}'])

        with self.assertRaises(runtime.LLMServiceError) as raised:
            self.execute(client, max_attempts=1)

        self.assertEqual(raised.exception.code, "MODEL_JSON_INVALID")
        self.assertEqual(raised.exception.attempts_used, 1)
        self.assertEqual(len(client.completions.calls), 1)

    def test_non_retryable_internal_error_stops_immediately(self):
        client = FakeClient([RuntimeError("unexpected"), '{"value": 7}'])

        with self.assertRaises(runtime.LLMServiceError) as raised:
            self.execute(client)

        self.assertEqual(raised.exception.code, "INTERNAL_ERROR")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(client.completions.calls), 1)

    def test_transient_connection_error_retries_then_succeeds(self):
        request = httpx.Request("POST", "https://example.invalid/chat")
        client = FakeClient(
            [
                APIConnectionError(request=request),
                '{"value": 7}',
            ]
        )

        result = self.execute(client)

        self.assertEqual(result.value, {"value": 7})
        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(len(client.completions.calls), 2)

    def test_missing_api_key_is_configuration_error(self):
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": ""}):
            with self.assertRaises(runtime.LLMServiceError) as raised:
                runtime.execute_json_request(
                    task="test_task",
                    messages=[],
                    validator=lambda payload: payload,
                    temperature=0.5,
                    timeout_seconds=25,
                )

        self.assertEqual(raised.exception.code, "CONFIGURATION_ERROR")
        self.assertEqual(raised.exception.status_code, 503)
        self.assertEqual(raised.exception.attempts_used, 0)

    def test_upstream_errors_have_distinct_codes_and_statuses(self):
        request = httpx.Request("POST", "https://example.invalid/chat")
        cases = [
            (
                APITimeoutError(request),
                "UPSTREAM_TIMEOUT",
                504,
                True,
            ),
            (
                APIConnectionError(request=request),
                "UPSTREAM_CONNECTION_ERROR",
                502,
                True,
            ),
            (
                RateLimitError(
                    "limited",
                    response=httpx.Response(429, request=request),
                    body=None,
                ),
                "UPSTREAM_RATE_LIMIT",
                503,
                True,
            ),
            (
                APIStatusError(
                    "unauthorized",
                    response=httpx.Response(401, request=request),
                    body=None,
                ),
                "UPSTREAM_AUTHENTICATION_FAILED",
                503,
                False,
            ),
            (
                APIStatusError(
                    "bad gateway",
                    response=httpx.Response(502, request=request),
                    body=None,
                ),
                "UPSTREAM_SERVER_ERROR",
                502,
                True,
            ),
        ]

        for exception, code, status_code, retryable in cases:
            with self.subTest(code=code):
                error = runtime._classify_exception(
                    exception,
                    request_id="classification-test",
                    attempts_used=1,
                    validation_stage="validation",
                )
                self.assertEqual(error.code, code)
                self.assertEqual(error.status_code, status_code)
                self.assertEqual(error.retryable, retryable)


class LLMApiContractTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(backend.app)

    def test_level_post_and_legacy_get_build_the_same_context(self):
        contexts = []

        def create_level_plan(context, request_id, max_attempts):
            contexts.append((context, max_attempts))
            return runtime.LLMExecutionResult(
                {"designNote": "test"},
                1,
                request_id,
            )

        payload = {
            "ideaText": "compact puzzle",
            "ideaId": "idea-1",
            "previousLevelPlan": json.dumps({"minPushes": 8}),
            "latestAdjustmentText": "fewer walls",
            "maxAttempts": 1,
        }

        with patch.object(backend, "create_level_plan", side_effect=create_level_plan):
            post_response = self.client.post(
                "/generate-level-plan",
                json=payload,
                headers={"X-Request-ID": "post-request"},
            )
            get_response = self.client.get(
                "/generate-level-plan",
                params=payload,
                headers={"X-Request-ID": "get-request"},
            )

        self.assertEqual(post_response.status_code, 200)
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(post_response.json(), {"designNote": "test"})
        self.assertEqual(contexts[0], contexts[1])
        self.assertEqual(post_response.headers["X-LLM-Attempts-Used"], "1")
        self.assertEqual(post_response.headers["X-Request-ID"], "post-request")

    def test_human_validation_post_and_legacy_get_use_same_text(self):
        adjustment_texts = []

        def validate_adjustment(adjustment_text, request_id):
            adjustment_texts.append(adjustment_text)
            return runtime.LLMExecutionResult(
                {"isClear": True},
                1,
                request_id,
            )

        with patch.object(
            backend,
            "create_human_adjustment_clarity_check",
            side_effect=validate_adjustment,
        ):
            post_response = self.client.post(
                "/validate-human-adjustment",
                json={"adjustmentText": "reduce walls"},
            )
            get_response = self.client.get(
                "/validate-human-adjustment",
                params={"adjustmentText": "reduce walls"},
            )

        self.assertEqual(post_response.status_code, 200)
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(adjustment_texts, ["reduce walls", "reduce walls"])
        self.assertEqual(post_response.json(), get_response.json())

    def test_standard_error_envelope_and_headers(self):
        error = runtime.LLMServiceError(
            "MODEL_VALIDATION_FAILED",
            "blueprint_validation",
            "minPushes=18 is outside 8-16",
            "error-request",
            True,
            2,
            502,
        )

        with patch.object(backend, "create_level_plan", side_effect=error):
            response = self.client.post(
                "/generate-level-plan",
                json={"ideaText": "test"},
                headers={"X-Request-ID": "error-request"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json(),
            {
                "detail": {
                    "code": "MODEL_VALIDATION_FAILED",
                    "stage": "blueprint_validation",
                    "message": "minPushes=18 is outside 8-16",
                    "requestId": "error-request",
                    "retryable": True,
                    "attemptsUsed": 2,
                }
            },
        )
        self.assertEqual(response.headers["X-Request-ID"], "error-request")
        self.assertEqual(response.headers["X-LLM-Attempts-Used"], "2")


if __name__ == "__main__":
    unittest.main()
