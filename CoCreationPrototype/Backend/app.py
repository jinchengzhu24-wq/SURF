import re
import uuid
from pathlib import Path
from typing import Literal

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from llm_client import LLMServiceError, generate_chat_reply


HOST = "127.0.0.1"
PORT = 8010
BACKEND_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BACKEND_DIR.parent / "Frontend"
MAX_MESSAGES = 20
MAX_MESSAGE_LENGTH = 2000
MAX_TOTAL_MESSAGE_LENGTH = 12000
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

SAMPLE_ROWS = [
    "############",
    "#..........#",
    "#..........#",
    "#..........#",
    "#...p......#",
    "#...s.t....#",
    "#..........#",
    "#..........#",
    "#..........#",
    "############",
]

SAMPLE_LEGEND = {
    "#": "wall",
    ".": "floor",
    "@": "water",
    "p": "player",
    "s": "box",
    "t": "target",
}

load_dotenv(BACKEND_DIR / ".env")


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[ChatMessage]


app = FastAPI(
    title="Sokoban Co-Creation Prototype",
    version="0.1.0",
)


@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    supplied_request_id = request.headers.get("X-Request-ID", "").strip()
    request_id = (
        supplied_request_id
        if REQUEST_ID_PATTERN.fullmatch(supplied_request_id)
        else uuid.uuid4().hex
    )
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    return response


@app.exception_handler(RequestValidationError)
async def handle_request_validation_error(
    request: Request,
    exception: RequestValidationError,
):
    return error_response(
        400,
        "INVALID_REQUEST",
        "The request body does not match the chat API contract.",
        request.state.request_id,
        False,
    )


@app.exception_handler(LLMServiceError)
async def handle_llm_service_error(request: Request, exception: LLMServiceError):
    response = error_response(
        exception.status_code,
        exception.code,
        exception.safe_message,
        exception.request_id,
        exception.retryable,
    )
    response.headers["X-LLM-Attempts-Used"] = str(exception.attempts_used)
    return response


@app.get("/api/sample")
def get_sample():
    return {
        "width": 12,
        "height": 10,
        "rows": list(SAMPLE_ROWS),
        "legend": dict(SAMPLE_LEGEND),
    }


@app.post("/api/chat")
def chat(payload: ChatRequest, request: Request, response: Response):
    messages = [message.model_dump() for message in payload.messages]
    validate_conversation(messages, request.state.request_id)
    execution = generate_chat_reply(
        messages,
        SAMPLE_ROWS,
        request.state.request_id,
    )
    response.headers["X-LLM-Attempts-Used"] = str(execution.attempts_used)
    return {
        "assistantMessage": execution.assistant_message,
        "requestId": execution.request_id,
    }


def validate_conversation(messages, request_id):
    if not messages:
        raise_api_error(
            "EMPTY_CONVERSATION",
            "At least one chat message is required.",
            request_id,
        )

    if len(messages) > MAX_MESSAGES:
        raise_api_error(
            "TOO_MANY_MESSAGES",
            f"A request may contain at most {MAX_MESSAGES} messages.",
            request_id,
        )

    if messages[-1]["role"] != "user":
        raise_api_error(
            "LAST_MESSAGE_MUST_BE_USER",
            "The final chat message must be from the user.",
            request_id,
        )

    total_length = 0

    for index, message in enumerate(messages):
        content = message["content"]

        if not isinstance(content, str) or not content.strip():
            raise_api_error(
                "EMPTY_MESSAGE",
                f"Message {index + 1} must not be empty.",
                request_id,
            )

        if len(content) > MAX_MESSAGE_LENGTH:
            raise_api_error(
                "MESSAGE_TOO_LONG",
                f"Message {index + 1} exceeds {MAX_MESSAGE_LENGTH} characters.",
                request_id,
            )

        total_length += len(content)

    if total_length > MAX_TOTAL_MESSAGE_LENGTH:
        raise_api_error(
            "CONVERSATION_TOO_LONG",
            f"Conversation text exceeds {MAX_TOTAL_MESSAGE_LENGTH} characters.",
            request_id,
        )


def raise_api_error(code, message, request_id):
    raise LLMServiceError(
        code,
        message,
        request_id,
        False,
        0,
        400,
    )


def error_response(status_code, code, message, request_id, retryable):
    return JSONResponse(
        status_code=status_code,
        content={
            "code": str(code),
            "message": str(message),
            "requestId": str(request_id),
            "retryable": bool(retryable),
        },
    )


app.mount(
    "/",
    StaticFiles(directory=FRONTEND_DIR, html=True, check_dir=False),
    name="prototype-frontend",
)


if __name__ == "__main__":
    uvicorn.run(app, host=HOST, port=PORT)

