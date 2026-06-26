import json
import os
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel

HOST = "127.0.0.1"
PORT = 8000
START_URL = f"http://{HOST}:{PORT}/generate-level-plan"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
BASE_DIR = Path(__file__).resolve().parent
STUDY_LOG_DIR = BASE_DIR / "study_logs"
STUDY_LOG_FILE = STUDY_LOG_DIR / "level_records.jsonl"

load_dotenv(BASE_DIR / ".env")


class LevelDesignPlan(BaseModel):
    minSolutionSteps: int
    maxSolutionSteps: int
    minWaterAreas: int
    maxWaterAreas: int
    minWallObstacleBlocks: int
    maxWallObstacleBlocks: int
    minPushes: int
    maxPushes: int
    minReversePulls: int
    maxReversePulls: int
    style: str
    archetype: str
    targetLayout: str
    obstacleStyle: str
    waterStyle: str
    designNote: str


DEFAULT_PLAN = {
    "minSolutionSteps": 22,
    "maxSolutionSteps": 42,
    "minWaterAreas": 1,
    "maxWaterAreas": 2,
    "minWallObstacleBlocks": 2,
    "maxWallObstacleBlocks": 3,
    "minPushes": 10,
    "maxPushes": 22,
    "minReversePulls": 18,
    "maxReversePulls": 34,
    "style": "hard classic choke route",
    "archetype": "bottleneck_corridor",
    "targetLayout": "split_pair",
    "obstacleStyle": "side_choke",
    "waterStyle": "side_pool",
    "designNote": "Hard two-box route with a forced choke point and separated goals.",
}

FALLBACK_PLANS = [
    DEFAULT_PLAN,
    {
        "minSolutionSteps": 22,
        "maxSolutionSteps": 40,
        "minWaterAreas": 1,
        "maxWaterAreas": 2,
        "minWallObstacleBlocks": 2,
        "maxWallObstacleBlocks": 3,
        "minPushes": 10,
        "maxPushes": 20,
        "minReversePulls": 18,
        "maxReversePulls": 32,
        "style": "guarded goal room",
        "archetype": "goal_room",
        "targetLayout": "clustered",
        "obstacleStyle": "goal_guard",
        "waterStyle": "corner_pool",
        "designNote": "Compact goal room pressure with clustered targets and a guarded approach.",
    },
    {
        "minSolutionSteps": 24,
        "maxSolutionSteps": 44,
        "minWaterAreas": 1,
        "maxWaterAreas": 2,
        "minWallObstacleBlocks": 2,
        "maxWallObstacleBlocks": 3,
        "minPushes": 12,
        "maxPushes": 22,
        "minReversePulls": 18,
        "maxReversePulls": 34,
        "style": "split route pressure",
        "archetype": "split_route",
        "targetLayout": "split_pair",
        "obstacleStyle": "central_baffle",
        "waterStyle": "route_divider",
        "designNote": "Separated goals and a central baffle encourage route planning.",
    },
]

RECENT_BLUEPRINT_LIMIT = 3
recent_blueprints = []
fallback_plan_index = 0
plan_history_lock = threading.Lock()
study_record_lock = threading.Lock()

LIMITS = {
    "minSolutionSteps": (18, 30),
    "maxSolutionSteps": (32, 50),
    "minWaterAreas": (1, 2),
    "maxWaterAreas": (1, 2),
    "minWallObstacleBlocks": (2, 2),
    "maxWallObstacleBlocks": (2, 3),
    "minPushes": (8, 16),
    "maxPushes": (14, 28),
    "minReversePulls": (14, 24),
    "maxReversePulls": (24, 40),
}

ENUMS = {
    "archetype": {"goal_room", "bottleneck_corridor", "split_route", "open_workshop"},
    "targetLayout": {"clustered", "split_pair", "edge_cluster"},
    "obstacleStyle": {"central_baffle", "side_choke", "goal_guard"},
    "waterStyle": {"corner_pool", "side_pool", "route_divider"},
}

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/record-level-start")
async def record_level_start(request: Request):
    return await append_level_record(request, "level-start")


@app.post("/record-level-end")
async def record_level_end(request: Request):
    return await append_level_record(request, "level-end")


@app.get("/generate-level-plan")
def generate_level_plan():
    return create_level_plan()


async def append_level_record(request: Request, default_event_type: str):
    data = await request.json()

    if not isinstance(data, dict):
        data = {"payload": data}

    data.setdefault("eventType", default_event_type)
    data["serverReceivedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        with STUDY_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(data, ensure_ascii=False))
            log_file.write("\n")

    return {
        "status": "ok",
        "eventType": data["eventType"],
        "logFile": str(STUDY_LOG_FILE),
    }


def create_level_plan():
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()

    if not api_key or api_key == "your_deepseek_api_key_here":
        return fallback_plan("DEEPSEEK_API_KEY is missing")

    try:
        model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
        base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        temperature = float(os.getenv("DEEPSEEK_TEMPERATURE", "0.9"))
        variation_seed = int(time.time() * 1000)
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=20.0)

        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a classic Sokoban level design director. Your job is "
                        "to create a high-level blueprint for an algorithmic Sokoban "
                        "level generator. Use classic design principles: compact rooms, "
                        "corridors, choke points, goal-room pressure, route planning, "
                        "reverse-design thinking, and deadlock avoidance. Do not copy "
                        "or reproduce any existing online level. Return only valid JSON. "
                        "Your archetype choice will select a hard local structure "
                        "template, so choose intentionally. Do not generate map rows, "
                        "coordinates, tile grids, markdown, or explanations."
                    ),
                },
                {
                    "role": "user",
                    "content": (
                        "Create a fresh, classic-inspired hard blueprint "
                        "for a 12x10 Sokoban level with exactly 2 boxes. The local "
                        "algorithm will enforce solvability, wall templates, water "
                        "placement, and tile rules. Pushes means box pushes, not "
                        "player walking moves. Choose values inside these "
                        "inclusive ranges: "
                        "minSolutionSteps 18-30, maxSolutionSteps 32-50, "
                        "minPushes 8-16, maxPushes 14-28, "
                        "minWaterAreas 1-2, maxWaterAreas 1-2, "
                        "minWallObstacleBlocks exactly 2, maxWallObstacleBlocks 2-3. "
                        "minReversePulls 14-24, maxReversePulls 24-40. "
                        "Each max value must be greater than or equal to its min value. "
                        "Choose exactly one archetype from: goal_room, "
                        "bottleneck_corridor, split_route, open_workshop. "
                        "Choose exactly one targetLayout from: clustered, split_pair, "
                        "edge_cluster. Choose exactly one obstacleStyle from: "
                        "central_baffle, side_choke, goal_guard. Choose exactly one "
                        "waterStyle from: corner_pool, side_pool, route_divider. "
                        "Use a short style label and a short designNote. Return exactly "
                        "these JSON keys: "
                        "minSolutionSteps, maxSolutionSteps, minPushes, "
                        "maxPushes, minWaterAreas, maxWaterAreas, minWallObstacleBlocks, "
                        "maxWallObstacleBlocks, minReversePulls, maxReversePulls, "
                        "style, archetype, targetLayout, obstacleStyle, waterStyle, "
                        "designNote. "
                        f"Variation seed: {variation_seed}. "
                        f"Avoid these recent blueprint combinations if possible: "
                        f"{get_recent_blueprint_hint()}."
                    ),
                },
            ],
            response_format={"type": "json_object"},
            temperature=temperature,
            stream=False,
        )

        content = response.choices[0].message.content
        plan = validate_plan(json.loads(content))
        remember_blueprint(plan)
        print(f"Generated level plan from DeepSeek using {model}: {plan}")
        return plan
    except Exception as exception:
        return fallback_plan(f"DeepSeek request failed: {exception}")


def validate_plan(plan):
    if plan is None:
        raise ValueError("model returned no parsed plan")

    data = plan.model_dump() if isinstance(plan, LevelDesignPlan) else dict(plan)

    for key, (minimum, maximum) in LIMITS.items():
        value = data.get(key)

        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{key} must be an integer")

        if value < minimum or value > maximum:
            raise ValueError(f"{key}={value} is outside {minimum}-{maximum}")

    if data["maxSolutionSteps"] < data["minSolutionSteps"]:
        raise ValueError("maxSolutionSteps must be >= minSolutionSteps")

    if data["maxPushes"] < data["minPushes"]:
        raise ValueError("maxPushes must be >= minPushes")

    if data["maxWaterAreas"] < data["minWaterAreas"]:
        raise ValueError("maxWaterAreas must be >= minWaterAreas")

    if data["maxWallObstacleBlocks"] < data["minWallObstacleBlocks"]:
        raise ValueError("maxWallObstacleBlocks must be >= minWallObstacleBlocks")

    if data["maxReversePulls"] < data["minReversePulls"]:
        raise ValueError("maxReversePulls must be >= minReversePulls")

    for key, allowed_values in ENUMS.items():
        value = str(data.get(key, "")).strip()

        if value not in allowed_values:
            raise ValueError(f"{key}={value} is not supported")

        data[key] = value

    style = str(data.get("style", "")).strip()
    if not style:
        style = DEFAULT_PLAN["style"]

    design_note = str(data.get("designNote", "")).strip()
    if not design_note:
        design_note = DEFAULT_PLAN["designNote"]

    return {
        "minSolutionSteps": data["minSolutionSteps"],
        "maxSolutionSteps": data["maxSolutionSteps"],
        "minPushes": data["minPushes"],
        "maxPushes": data["maxPushes"],
        "minWaterAreas": data["minWaterAreas"],
        "maxWaterAreas": data["maxWaterAreas"],
        "minWallObstacleBlocks": data["minWallObstacleBlocks"],
        "maxWallObstacleBlocks": data["maxWallObstacleBlocks"],
        "minReversePulls": data["minReversePulls"],
        "maxReversePulls": data["maxReversePulls"],
        "style": style[:80],
        "archetype": data["archetype"],
        "targetLayout": data["targetLayout"],
        "obstacleStyle": data["obstacleStyle"],
        "waterStyle": data["waterStyle"],
        "designNote": design_note[:160],
    }


def fallback_plan(reason):
    plan = get_next_fallback_plan()
    remember_blueprint(plan)
    print(f"Generated level plan from fallback: {reason}")
    return plan


def get_next_fallback_plan():
    global fallback_plan_index

    with plan_history_lock:
        plan = FALLBACK_PLANS[fallback_plan_index % len(FALLBACK_PLANS)].copy()
        fallback_plan_index += 1

    return plan


def remember_blueprint(plan):
    key = get_blueprint_key(plan)

    with plan_history_lock:
        if key in recent_blueprints:
            recent_blueprints.remove(key)

        recent_blueprints.append(key)

        while len(recent_blueprints) > RECENT_BLUEPRINT_LIMIT:
            recent_blueprints.pop(0)


def get_recent_blueprint_hint():
    with plan_history_lock:
        if not recent_blueprints:
            return "none"

        return "; ".join(recent_blueprints)


def get_blueprint_key(plan):
    return "|".join(
        [
            str(plan.get("archetype", "")),
            str(plan.get("targetLayout", "")),
            str(plan.get("obstacleStyle", "")),
            str(plan.get("waterStyle", "")),
        ]
    )


def open_browser():
    time.sleep(1)
    webbrowser.open(START_URL)


if __name__ == "__main__":
    print(f"Starting backend at http://{HOST}:{PORT}")
    print(f"Opening {START_URL}")
    threading.Thread(target=open_browser, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT)
