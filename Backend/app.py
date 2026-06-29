import html
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
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pydantic import BaseModel

HOST = "127.0.0.1"
PORT = 8000
START_URL = f"http://{HOST}:{PORT}/generate-level-plan"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_BASE_URL = "https://api.deepseek.com"
BASE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
FRONTEND_DIR = PROJECT_DIR / "Frontend"
STUDY_LOG_DIR = BASE_DIR / "study_logs"
STUDY_LOG_FILE = STUDY_LOG_DIR / "level_records.jsonl"
SURVEY_LOG_FILE = STUDY_LOG_DIR / "survey_responses.jsonl"

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
app.mount(
    "/frontend",
    StaticFiles(directory=FRONTEND_DIR, html=True, check_dir=False),
    name="frontend",
)

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


@app.post("/record-survey-response")
async def record_survey_response(request: Request):
    return await append_survey_record(request)


@app.get("/level-records", response_class=PlainTextResponse)
def get_level_records():
    if not STUDY_LOG_FILE.exists():
        return ""

    return STUDY_LOG_FILE.read_text(encoding="utf-8")


@app.get("/survey-records", response_class=PlainTextResponse)
def get_survey_records():
    if not SURVEY_LOG_FILE.exists():
        return ""

    return SURVEY_LOG_FILE.read_text(encoding="utf-8")


@app.get("/survey-records-data")
def get_survey_records_data():
    responses, malformed_count = read_survey_response_events()
    return build_survey_records_payload(responses, malformed_count)


@app.get("/level-records-view", response_class=HTMLResponse)
def get_level_records_view(cleared: int = 0):
    target = "/frontend/"

    if cleared == 1:
        target += "?cleared=1"

    return RedirectResponse(target, status_code=302)


@app.get("/level-records-dashboard")
def get_level_records_dashboard(cleared: int = 0):
    target = "/frontend/"

    if cleared == 1:
        target += "?cleared=1"

    return RedirectResponse(target, status_code=302)


@app.get("/level-records-data")
def get_level_records_data():
    events, malformed_count = read_level_record_events()
    levels = merge_level_records(events)
    payload = build_level_records_payload(events, levels, malformed_count)
    survey_responses, survey_malformed_count = read_survey_response_events()
    survey_payload = build_survey_records_payload(
        survey_responses,
        survey_malformed_count,
    )
    payload["surveySummary"] = survey_payload["summary"]
    payload["surveyResponses"] = survey_payload["responses"]
    payload["surveyMalformedCount"] = survey_payload["malformedCount"]
    return payload


@app.get("/level-records-legacy", response_class=HTMLResponse)
def get_level_records_legacy(cleared: int = 0):
    events, malformed_count = read_level_record_events()
    levels = merge_level_records(events)
    return render_level_records_view(events, levels, malformed_count, cleared == 1)


@app.post("/clear-level-records")
def clear_level_records():
    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        STUDY_LOG_FILE.write_text("", encoding="utf-8")
        SURVEY_LOG_FILE.write_text("", encoding="utf-8")

    return RedirectResponse("/level-records-view?cleared=1", status_code=303)


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


async def append_survey_record(request: Request):
    data = await request.json()

    if not isinstance(data, dict):
        data = {"payload": data}

    data.setdefault("eventType", "survey-response")
    data["serverReceivedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    STUDY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    with study_record_lock:
        with SURVEY_LOG_FILE.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(data, ensure_ascii=False))
            log_file.write("\n")

    return {
        "status": "ok",
        "eventType": data["eventType"],
        "logFile": str(SURVEY_LOG_FILE),
    }


def read_level_record_events():
    return read_jsonl_records(STUDY_LOG_FILE)


def read_survey_response_events():
    return read_jsonl_records(SURVEY_LOG_FILE)


def read_jsonl_records(path):
    if not path.exists():
        return [], 0

    records = []
    malformed_count = 0

    with path.open("r", encoding="utf-8") as log_file:
        for line in log_file:
            line = line.strip()

            if not line:
                continue

            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                malformed_count += 1
                continue

            if isinstance(data, dict):
                records.append(data)
            else:
                malformed_count += 1

    return records, malformed_count


def merge_level_records(events):
    levels = {}

    for index, event in enumerate(events):
        level_run_id = str(event.get("levelRunId") or f"missing-run-{index + 1}")

        if level_run_id not in levels:
            levels[level_run_id] = {
                "levelRunId": level_run_id,
                "start": None,
                "end": None,
                "events": [],
                "order": index,
            }

        level = levels[level_run_id]
        level["events"].append(event)
        event_type = event.get("eventType")

        if event_type == "level-start":
            level["start"] = event
        elif event_type == "level-end":
            level["end"] = event

    return sorted(
        levels.values(),
        key=lambda level: (
            get_level_sort_value(level),
            level["order"],
        ),
    )


def build_level_records_payload(events, levels, malformed_count):
    session_ids = {
        event.get("sessionId")
        for event in events
        if event.get("sessionId")
    }
    completed_count = 0
    missing_end_count = 0
    restarted_count = 0
    total_duration_seconds = 0.0
    ended_level_count = 0
    total_moves = 0
    total_pushes = 0
    source_counts = {}

    for level in levels:
        start = get_level_start(level)
        end = get_level_end(level)
        source = value_or_dash(get_record_value(start, "source"))
        source_counts[source] = source_counts.get(source, 0) + 1

        if not end:
            missing_end_count += 1
            continue

        if get_record_value(end, "completed"):
            completed_count += 1

        if get_record_value(end, "endReason") == "restarted":
            restarted_count += 1

        duration = get_record_value(end, "durationSeconds")

        if isinstance(duration, (int, float)):
            total_duration_seconds += duration
            ended_level_count += 1

        move_count = get_record_value(end, "moveCount")
        push_count = get_record_value(end, "pushCount")

        if isinstance(move_count, int):
            total_moves += move_count

        if isinstance(push_count, int):
            total_pushes += push_count

    average_duration_seconds = (
        total_duration_seconds / ended_level_count
        if ended_level_count > 0
        else 0
    )

    return {
        "summary": {
            "eventCount": len(events),
            "levelCount": len(levels),
            "sessionCount": len(session_ids),
            "completedCount": completed_count,
            "missingEndCount": missing_end_count,
            "restartedCount": restarted_count,
            "malformedCount": malformed_count,
            "totalMoves": total_moves,
            "totalPushes": total_pushes,
            "averageDurationSeconds": round(average_duration_seconds, 2),
            "sourceCounts": source_counts,
        },
        "events": events,
        "levels": levels,
        "malformedCount": malformed_count,
        "logFile": str(STUDY_LOG_FILE),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def build_survey_records_payload(responses, malformed_count):
    session_ids = {
        response.get("sessionId")
        for response in responses
        if response.get("sessionId")
    }
    survey_counts = {}
    total_duration_seconds = 0.0
    duration_count = 0
    answer_count = 0

    for response in responses:
        survey_id = value_or_dash(response.get("surveyId"))
        survey_counts[survey_id] = survey_counts.get(survey_id, 0) + 1

        duration = response.get("durationSeconds")

        if isinstance(duration, (int, float)):
            total_duration_seconds += duration
            duration_count += 1

        answers = response.get("answers")

        if isinstance(answers, list):
            answer_count += len(answers)

    average_duration_seconds = (
        total_duration_seconds / duration_count
        if duration_count > 0
        else 0
    )

    sorted_responses = sorted(
        responses,
        key=lambda response: response.get("serverReceivedAt") or response.get("timestamp") or "",
        reverse=True,
    )

    return {
        "summary": {
            "responseCount": len(responses),
            "sessionCount": len(session_ids),
            "answerCount": answer_count,
            "averageDurationSeconds": round(average_duration_seconds, 2),
            "malformedCount": malformed_count,
            "surveyCounts": survey_counts,
        },
        "responses": sorted_responses,
        "malformedCount": malformed_count,
        "logFile": str(SURVEY_LOG_FILE),
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def render_level_records_view(events, levels, malformed_count, cleared):
    session_ids = {
        event.get("sessionId")
        for event in events
        if event.get("sessionId")
    }
    completed_count = sum(1 for level in levels if get_level_end(level) and get_level_end(level).get("completed"))
    missing_end_count = sum(1 for level in levels if not get_level_end(level))
    rows_html = "\n".join(render_level_row(level) for level in levels)

    if not rows_html:
        rows_html = (
            '<tr><td colspan="16" class="empty">'
            "No level records found yet."
            "</td></tr>"
        )

    notice_html = ""

    if cleared:
        notice_html = '<div class="notice">Records cleared.</div>'

    return f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Sokoban Level Records</title>
    <style>
        body {{
            margin: 24px;
            font-family: Arial, Helvetica, sans-serif;
            color: #20242a;
            background: #f5f7fb;
        }}
        h1 {{
            margin: 0 0 8px;
            font-size: 28px;
        }}
        .meta {{
            margin-bottom: 18px;
            color: #5e6875;
        }}
        .summary {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin: 18px 0;
        }}
        .stat {{
            min-width: 130px;
            padding: 12px 14px;
            border: 1px solid #d9e0ea;
            border-radius: 6px;
            background: #ffffff;
        }}
        .stat strong {{
            display: block;
            font-size: 22px;
            color: #17202a;
        }}
        .toolbar {{
            display: flex;
            align-items: center;
            gap: 14px;
            margin: 18px 0;
        }}
        .toolbar a {{
            color: #175cd3;
            text-decoration: none;
        }}
        .toolbar form {{
            margin: 0;
        }}
        .danger-button {{
            padding: 7px 11px;
            border: 1px solid #c7372f;
            border-radius: 4px;
            color: #ffffff;
            background: #c7372f;
            cursor: pointer;
            font-size: 13px;
        }}
        .danger-button:hover {{
            background: #a82d27;
        }}
        .notice {{
            margin: 12px 0 18px;
            padding: 10px 12px;
            border: 1px solid #b7dfc1;
            border-radius: 6px;
            color: #14532d;
            background: #eaf8ee;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: #ffffff;
            border: 1px solid #d9e0ea;
        }}
        th, td {{
            padding: 8px 10px;
            border-bottom: 1px solid #e6ebf2;
            vertical-align: top;
            text-align: left;
            font-size: 13px;
        }}
        th {{
            position: sticky;
            top: 0;
            background: #edf2f7;
            z-index: 1;
        }}
        tr:nth-child(even) td {{
            background: #fafcff;
        }}
        .status-completed {{
            color: #16703b;
            font-weight: 700;
        }}
        .status-missing {{
            color: #a45c00;
            font-weight: 700;
        }}
        .map {{
            margin: 0;
            padding: 8px;
            min-width: 160px;
            border-radius: 4px;
            background: #17202a;
            color: #f7fafc;
            font-family: Consolas, "Courier New", monospace;
            font-size: 12px;
            line-height: 1.25;
            white-space: pre;
        }}
        .small {{
            color: #697586;
            font-size: 12px;
        }}
        .empty {{
            padding: 24px;
            text-align: center;
            color: #697586;
        }}
    </style>
</head>
<body>
    <h1>Sokoban Level Records</h1>
    <div class="meta">Human-readable view generated from <code>{escape_text(str(STUDY_LOG_FILE))}</code>.</div>
    {notice_html}
    <div class="summary">
        <div class="stat"><strong>{len(events)}</strong>events</div>
        <div class="stat"><strong>{len(levels)}</strong>levels</div>
        <div class="stat"><strong>{len(session_ids)}</strong>sessions</div>
        <div class="stat"><strong>{completed_count}</strong>completed</div>
        <div class="stat"><strong>{missing_end_count}</strong>missing end</div>
        <div class="stat"><strong>{malformed_count}</strong>malformed</div>
    </div>
    <div class="toolbar">
        <a href="/level-records">Raw JSONL</a>
        <a href="/docs">API Docs</a>
        <form method="post" action="/clear-level-records" onsubmit="return confirm('Clear all level records? This cannot be undone.');">
            <button class="danger-button" type="submit">Clear Records</button>
        </form>
    </div>
    <table>
        <thead>
            <tr>
                <th>Session</th>
                <th>Level</th>
                <th>Source</th>
                <th>Status</th>
                <th>Duration</th>
                <th>Moves</th>
                <th>Pushes</th>
                <th>Restarts</th>
                <th>Solution</th>
                <th>Solver Pushes</th>
                <th>Attempts</th>
                <th>Wall</th>
                <th>Water</th>
                <th>Dead Corner</th>
                <th>Map Hash</th>
                <th>Map</th>
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>
</body>
</html>"""


def render_level_row(level):
    start = get_level_start(level)
    end = get_level_end(level)
    source = value_or_dash(get_record_value(start, "source"))
    completed = get_record_value(end, "completed") if end else None
    status_class = "status-completed" if completed else "status-missing"
    status_text = "completed" if completed else "missing end"

    if end and not completed:
        status_text = value_or_dash(get_record_value(end, "endReason"))

    structure = get_record_value(start, "structure") or {}

    return f"""<tr>
    <td>{escape_text(short_id(get_record_value(start, "sessionId") or get_record_value(end, "sessionId")))}</td>
    <td>{escape_text(value_or_dash(get_record_value(start, "levelIndex") or get_record_value(end, "levelIndex")))}</td>
    <td>{escape_text(source)}</td>
    <td class="{status_class}">{escape_text(status_text)}</td>
    <td>{escape_text(format_seconds(get_record_value(end, "durationSeconds")))}</td>
    <td>{escape_text(value_or_dash(get_record_value(end, "moveCount")))}</td>
    <td>{escape_text(value_or_dash(get_record_value(end, "pushCount")))}</td>
    <td>{escape_text(value_or_dash(get_record_value(end, "restartCount")))}</td>
    <td>{escape_text(value_or_dash(get_record_value(start, "solutionSteps")))}</td>
    <td>{escape_text(value_or_dash(get_record_value(start, "solverPushes")))}</td>
    <td>{escape_text(value_or_dash(get_record_value(start, "generationAttempts")))}</td>
    <td>{escape_text(format_ratio(structure.get("wallDensity")))}</td>
    <td>{escape_text(format_ratio(structure.get("waterDensity")))}</td>
    <td>{escape_text(format_ratio(structure.get("deadCornerRisk")))}</td>
    <td><span class="small">{escape_text(value_or_dash(structure.get("mapHash")))}</span></td>
    <td><pre class="map">{render_map_rows(get_record_value(start, "rows"))}</pre></td>
</tr>"""


def get_level_sort_value(level):
    start = get_level_start(level)
    end = get_level_end(level)
    level_index = get_record_value(start, "levelIndex") or get_record_value(end, "levelIndex")

    if isinstance(level_index, int):
        return level_index

    return 999999


def get_level_start(level):
    return level.get("start")


def get_level_end(level):
    return level.get("end")


def get_record_value(record, key):
    if not isinstance(record, dict):
        return None

    return record.get(key)


def render_map_rows(rows):
    if not rows:
        return "-"

    return escape_text("\n".join(str(row) for row in rows))


def format_seconds(value):
    if isinstance(value, (int, float)):
        return f"{value:.1f}s"

    return "-"


def format_ratio(value):
    if isinstance(value, (int, float)):
        return f"{value:.3f}"

    return "-"


def value_or_dash(value):
    if value is None or value == "":
        return "-"

    return str(value)


def short_id(value):
    if not value:
        return "-"

    value = str(value)
    return value[:8]


def escape_text(value):
    return html.escape(str(value), quote=True)


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
