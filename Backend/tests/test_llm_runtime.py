import json
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient
import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from sokoban_backend.dependencies import BackendSettings, LLMGateway
from sokoban_backend.llm import runtime
from tests.fakes import (
    FakeClock,
    FakeLLMGateway,
    FakeOpenAIClient,
    RecordingBlueprintPlannerService,
    RecordingEventLogger,
    create_test_app,
    create_test_container,
)


class LLMRuntimeTests(unittest.TestCase):
    def execute(self, client: FakeOpenAIClient, *, api_key: str = "test-key", **overrides):
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

        clock = FakeClock()
        settings = BackendSettings(deepseek_api_key=api_key)
        logger = RecordingEventLogger(settings=settings, clock=clock.as_clock())
        gateway = LLMGateway(
            settings=settings,
            client_factory=client.as_factory(),
            clock=clock.as_clock(),
            event_logger=logger,
        )
        return gateway.execute_json_request(**arguments)

    def test_invalid_json_is_repaired_once(self):
        client = FakeOpenAIClient(["not-json", '{"value": 7}'])
        result = self.execute(client)

        self.assertEqual(result.value, {"value": 7})
        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(len(client.completions.calls), 2)
        repair_message = client.completions.calls[1]["messages"][-1]["content"]
        self.assertIn("failed validation", repair_message)
        self.assertIn("valid JSON", repair_message)

    def test_thinking_mode_is_forwarded_when_configured(self):
        client = FakeOpenAIClient(['{"value": 7}'])
        self.execute(
            client,
            max_attempts=1,
            thinking_mode="disabled",
        )

        self.assertEqual(
            client.completions.calls[0]["extra_body"],
            {"thinking": {"type": "disabled"}},
        )

    def test_validation_error_is_included_in_repair_request(self):
        client = FakeOpenAIClient(['{"value": 18}', '{"value": 12}'])

        def validate(payload):
            if payload["value"] > 16:
                raise ValueError("minPushes=18 is outside 8-16")

            return payload

        result = self.execute(client, validator=validate)

        self.assertEqual(result.value["value"], 12)
        self.assertEqual(result.attempts_used, 2)
        repair_message = client.completions.calls[1]["messages"][-1]["content"]
        self.assertIn("minPushes=18 is outside 8-16", repair_message)

    def test_non_english_string_is_repaired_once(self):
        client = FakeOpenAIClient(
            [
                '{"title":"Central Maze","nested":{"promptText":"使用迷宫路线"}}',
                '{"title":"Central Maze","nested":{"promptText":"Use a maze route"}}',
            ]
        )

        result = self.execute(client)

        self.assertEqual(result.value["nested"]["promptText"], "Use a maze route")
        self.assertEqual(result.attempts_used, 2)
        repair_message = client.completions.calls[1]["messages"][-1]["content"]
        self.assertIn("English ASCII text only", repair_message)
        self.assertIn("$.nested.promptText", repair_message)

    def test_common_typographic_punctuation_is_normalized(self):
        client = FakeOpenAIClient([
            '{"summary":"A wider route— with a ' + chr(0x2019) + 'friend"}',
        ])

        result = self.execute(client, max_attempts=1)

        self.assertEqual(result.value["summary"], "A wider route- with a 'friend")

    def test_repeated_non_english_strings_are_rejected(self):
        client = FakeOpenAIClient(
            [
                '{"title":"中央迷宫"}',
                '{"title":"迷宫路线"}',
            ]
        )

        with self.assertRaises(runtime.LLMServiceError) as raised:
            self.execute(client)

        self.assertEqual(raised.exception.code, "MODEL_VALIDATION_FAILED")
        self.assertEqual(raised.exception.attempts_used, 2)
        self.assertIn("English ASCII text only", raised.exception.safe_message)
        self.assertEqual(len(client.completions.calls), 2)

    def test_model_budget_never_exceeds_requested_single_attempt(self):
        client = FakeOpenAIClient(["not-json", '{"value": 7}'])

        with self.assertRaises(runtime.LLMServiceError) as raised:
            self.execute(client, max_attempts=1)

        self.assertEqual(raised.exception.code, "MODEL_JSON_INVALID")
        self.assertEqual(raised.exception.attempts_used, 1)
        self.assertEqual(len(client.completions.calls), 1)

    def test_non_retryable_internal_error_stops_immediately(self):
        client = FakeOpenAIClient([RuntimeError("unexpected"), '{"value": 7}'])

        with self.assertRaises(runtime.LLMServiceError) as raised:
            self.execute(client)

        self.assertEqual(raised.exception.code, "INTERNAL_ERROR")
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(client.completions.calls), 1)

    def test_transient_connection_error_retries_then_succeeds(self):
        request = httpx.Request("POST", "https://example.invalid/chat")
        client = FakeOpenAIClient(
            [
                APIConnectionError(request=request),
                '{"value": 7}',
            ]
        )

        result = self.execute(client)

        self.assertEqual(result.value, {"value": 7})
        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(len(client.completions.calls), 2)

    def test_restricted_retry_codes_do_not_repeat_timeout(self):
        request = httpx.Request("POST", "https://example.invalid/chat")
        client = FakeOpenAIClient(
            [
                APITimeoutError(request),
                '{"value": 7}',
            ]
        )

        with self.assertRaises(runtime.LLMServiceError) as raised:
            self.execute(
                client,
                retry_error_codes={
                    "MODEL_JSON_INVALID",
                    "MODEL_VALIDATION_FAILED",
                },
            )

        self.assertEqual(raised.exception.code, "UPSTREAM_TIMEOUT")
        self.assertEqual(raised.exception.attempts_used, 1)
        self.assertEqual(len(client.completions.calls), 1)

    def test_restricted_retry_codes_still_repair_invalid_json(self):
        client = FakeOpenAIClient(["not-json", '{"value": 7}'])

        result = self.execute(
            client,
            retry_error_codes={
                "MODEL_JSON_INVALID",
                "MODEL_VALIDATION_FAILED",
            },
        )

        self.assertEqual(result.value, {"value": 7})
        self.assertEqual(result.attempts_used, 2)
        self.assertEqual(len(client.completions.calls), 2)

    def test_missing_api_key_is_configuration_error(self):
        client = FakeOpenAIClient()
        with self.assertRaises(runtime.LLMServiceError) as raised:
            self.execute(
                client,
                api_key="",
                messages=[],
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
                error = runtime.classify_llm_exception(
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
        self.temp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_level_post_and_legacy_get_build_the_same_context(self):
        contexts = []

        def handle_level_plan(context, request_id, max_attempts):
            contexts.append((context, max_attempts))
            return runtime.LLMExecutionResult(
                {"designNote": "test"},
                1,
                request_id,
            )

        gateway = FakeLLMGateway()
        planner = RecordingBlueprintPlannerService(
            gateway,
            level_plan_handler=handle_level_plan,
        )
        container = create_test_container(
            self.tmp_path,
            llm_gateway=gateway,
            blueprint_service=planner,
        )
        payload = {
            "ideaText": "compact puzzle",
            "ideaId": "idea-1",
            "previousLevelPlan": json.dumps({"minPushes": 8}),
            "latestAdjustmentText": "fewer walls",
            "maxAttempts": 1,
        }

        with TestClient(create_test_app(self.tmp_path, container=container)) as client:
            post_response = client.post(
                "/generate-level-plan",
                json=payload,
                headers={"X-Request-ID": "post-request"},
            )
            get_response = client.get(
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

        gateway = FakeLLMGateway()
        planner = RecordingBlueprintPlannerService(
            gateway,
            clarity_check_handler=validate_adjustment,
        )
        container = create_test_container(
            self.tmp_path,
            llm_gateway=gateway,
            blueprint_service=planner,
        )

        with TestClient(create_test_app(self.tmp_path, container=container)) as client:
            post_response = client.post(
                "/validate-human-adjustment",
                json={"adjustmentText": "reduce walls"},
            )
            get_response = client.get(
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
        app = create_test_app(self.tmp_path, llm_outcomes=[error])

        with TestClient(app) as client:
            response = client.post(
                "/generate-level-plan",
                json={"ideaText": "test"},
                headers={
                    "X-Request-ID": "error-request",
                    "Origin": "http://localhost:8000",
                },
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
        exposed_headers = response.headers["Access-Control-Expose-Headers"]
        self.assertIn("X-Request-ID", exposed_headers)
        self.assertIn("X-LLM-Attempts-Used", exposed_headers)


if __name__ == "__main__":
    unittest.main()
