# Repository Guidelines

## Project Structure & Module Organization

This repository combines a Unity 2D Sokoban client with a Python service. Unity gameplay code lives in `Assets/Scripts/`, grouped by feature (`Level/`, `LLM/`, `Player/`, `Study/`, and `Menu/`). Scenes are under `Assets/Scenes/`; prefabs, tiles, animations, fonts, and WebGL templates remain in their corresponding `Assets/` folders. Keep every Unity asset paired with its `.meta` file.

`Backend/` contains the FastAPI application, LLM runtime and prompts, plus Python tests. `Frontend/` is the static study dashboard served at `/frontend/`. Unity package and editor settings live in `Packages/` and `ProjectSettings/`. Treat `Library/`, `Temp/`, `Logs/`, generated solution files, and `WebGLBuild/` as generated output.

## Current Project Status (2026-08-02)

The active matchmaking route is:

```text
Menu -> Online_Lobby -> Match_Briefing -> Competition_Mode -> AI_Asistant_Mode
                                                              -> PC -> PC_Design -> PC_Level
                                                              -> DG -> DG_Level
     -> complete the generated level -> Challenge_Waiting
     -> Online_Level -> Match_Result -> Questionnaire(Online) -> Menu
```

Keep the existing scene spelling `AI_Asistant_Mode`; changing it requires coordinated scene, script, and Build Settings updates. The PC branch is separate from DG and Creative Workshop data. `PC_Level` reads only the sketch saved by `PC_Design`.

### PC_Design

- The editable sketch is fixed at `12x10` and uses `#` for user walls, `s` for box starts, `t` for targets, and spaces for editable cells.
- A valid submission contains one or two matching `s`/`t` pairs, one enclosed connected activity area of at least 56 cells, and no `s` orthogonally adjacent to a wall.
- Check/Submit also requires room for at least one `2x2` water area and three generated internal wall cells. No water tile may have a wall directly above it. Generated wall cells must be at least one orthogonal cell away from the outer shell; diagonal contact is allowed.
- `PCDesignContext` persists the submitted sketch so Back from `PC_Level` restores it.
- Relevant files are `Assets/Scripts/Level/PCLevelSketchController.cs`, `PCDesignFeasibilityValidator.cs`, and `PCDesignContext.cs`.

### PC_Level generation

- Unity calls `POST /generate-pc-level` at `http://111.231.136.4:8000/generate-pc-level`. The serialized scene and client timeout are both 30 seconds.
- The request includes `width`, `height`, `sketchRows`, `competitionMode`, optional retry context, and `maxAttempts`. The response remains `{"rows":[...10 rows...]}`.
- The backend normalizes the request once, enumerates legal `2x2` through `4x4` water rectangles, and checks at most six water choices.
- For each checked water choice, the backend solves the no-new-wall map once and records a complete solution trace. Generated walls are selected only from cells never occupied by the player or boxes in that trace, so the recorded solution remains valid after the walls are added.
- Cheap greedy scoring constructs at most six complete layouts. Five internal walls are preferred, four are retained as an intermediate fallback, and the guaranteed minimum is three internal walls plus one water area. Bent, split, or dispersed wall shapes rank above a straight line.
- Generated walls may not cover fixed user tiles, touch a box start, divide the activity area, sit orthogonally next to an outer-shell wall, appear directly above water, or contain a complete `2x2` block made only from generated wall cells. User-authored wall blocks are not subject to the generated-wall `2x2` restriction. There is currently no 48-cell post-generation activity-area minimum; the PC_Design submission minimum remains 56.
- Competition Mode is a hard generated-wall topology rule using orthogonal adjacency. `competitive` allows each generated-wall component to contain at most two tiles; `supportive` requires every generated internal wall tile to belong to one connected component. Player-authored PC sketch walls are excluded from this check.
- The LLM receives complete layouts and returns only `{"layoutCandidateId": n}`. It cannot mix player, water, or wall IDs from different candidates.
- PC generation makes one model call with a 15-second backend timeout. Invalid JSON, an unknown ID, timeout, connection failure, or other `LLMServiceError` immediately selects the highest-ranked safe candidate instead of making a second model call.
- Do not restore the deleted wall-combination enumeration, counterfactual wall-impact search, or the requirement that walls increase shortest steps or pushes. Stability and guaranteed solvability are the current priority.
- Unity still validates the full rows and runs `LevelSolver` with `maxSearchStates: 300000` before loading. Its minimum generated feature check is three new wall tiles and one rectangular water area.
- Relevant files are `Backend/app.py` (`normalize_pc_level_request`, `build_pc_safe_layout_candidates`, and `create_pc_level_candidate`), `Backend/prompt.py`, `Assets/Scripts/LLM/PCLevelExpansionClient.cs`, `Assets/Scripts/Level/PCLevelExpansionController.cs`, and `PCLevelCandidateValidator.cs`.

### DG_Level generation

- `LLMLevelDesignClient` sends the selected `competitionMode` while generating a DG level plan. Divider corridors are disabled in both matchmaking modes because they cannot preserve the required internal-wall topology.
- `LevelGenerator` applies the same orthogonal-adjacency rule locally in `DG_Level`: competitive wall shapes contain one or two tiles and separate blocks cannot join into a component larger than two; supportive blocks must attach to the already generated internal-wall component.
- These DG restrictions apply to generated wall obstacles, not the closed or irregular outer shell.

### Online matchmaking and challenge exchange

- `MenuController.OpenMatchmaking()` loads `Online_Lobby`. Players create or join an anonymous two-player room, enter `Match_Briefing`, and move to `Competition_Mode` after both Ready flags are true.
- The backend uses a single-process in-memory room store with six-character room codes, anonymous player tokens, one-second HTTP polling, lazy 30-minute expiry, and the states `waiting_for_opponent`, `briefing`, `choosing_mode`, `waiting_for_challenges`, `challenges_ready`, `waiting_for_results`, `results_ready`, and `cancelled`.
- Room endpoints are `POST /online/rooms`, `POST /online/rooms/join`, `GET /online/rooms/{matchId}`, and the authenticated `/ready`, `/challenge`, `/result`, and `/leave` endpoints. Authenticated requests use `X-Player-Token`.
- Accepted room state changes are also appended to `Backend/study_logs/online_match_events.jsonl` for the MatchMaking dashboard. The event log stores room/player numbers, modes, full challenge rows, results, and server timestamps, but never player tokens. GET polling and identical retries do not add events.
- `OnlineMatchContext` is runtime-only. It stores the room identity, pending own rows and mode metadata, opponent rows, room state, and results. Before entering `Questionnaire(Online)`, it temporarily preserves `matchId`, room code, and player number until the survey succeeds so the response can be joined to the match. Refreshing WebGL or restarting the server does not restore a match.
- In an online room, completing `PC_Level` or `DG_Level` stages the final rows and enters `Challenge_Waiting`. Without a valid online context, both scenes keep their original standalone completion behavior.
- Challenge submissions include `rows`, `competitionMode`, and `aiAssistantMode`. Identical repeats are idempotent; changed rows or metadata are rejected after the first accepted submission. The server returns only the opponent rows to each authenticated player after both submissions.
- `Online_Level` validates and solves the opponent rows locally before loading them. It uses `Player2` with arrow keys; PC/DG continue to use `Player` with WASD. Time and successful moves accumulate from first control until solve and are not reset by `R`.
- Results include `durationSeconds`, `moveCount`, and `minimumMoves`. `Match_Result` displays each challenge's modes and both runs and polls while one result is missing. Continue leaves the room, clears `OnlineMatchContext`, and loads `Questionnaire(Online)`.
- `Questionnaire(Online)` reuses `QuestionnaireController` with survey ID `online_post_match_survey`, does not require another nickname, and targets `Menu`. It uses three discrete 1-to-5 sliders with visible integer ticks, circular handles, live score boxes, and a valid default score of 3. Scores remain compatible with the existing answer envelope through `optionIndex`, `score_N` in `optionId`, and the numeric `optionText`. It must load Menu only after `/record-survey-response` succeeds; a failed submission remains on the questionnaire for retry.
- `/frontend/` is the MatchMaking dashboard and `/frontend/train.html` preserves the original Train dashboard. Both share the existing visual system and provide a top-level flow switch. MatchMaking records are exposed through `/matchmaking-records-data`; password-protected deletion uses `/delete-online-match` and `/clear-matchmaking-records` without changing Train records.
- `ProjectSettings/ProjectSettings.asset` currently has `runInBackground: 0`. A background WebGL tab may pause polling; dual-browser tests must refocus the waiting result page and allow another polling interval before treating missing data as a role-mapping bug.
- Online scene UI is serialized and statically visible in the editor. Relevant files are under `Assets/Scenes/Matchmaking/Online/`, `Assets/Scripts/Online/`, and `Assets/Scripts/Study/QuestionnaireController.cs`. All six online scenes are enabled in `ProjectSettings/EditorBuildSettings.asset`.

### Deployment and verification state

- Last remote verification on 2026-08-02: `http://111.231.136.4:8000/ready` returned ready. The deployed backend contains the PC `5/4/3` thresholds, water-clearance and generated-wall `2x2` rules, competition-mode topology, challenge exchange, and result endpoints.
- The remote WebGL at `http://111.231.136.4:8000/game/` currently contains the online scenes through `Match_Result`. Its current cache key is `online-20260731-5`; the deployed `WebGLBuild.data` was last uploaded on 2026-07-31. The newly added `Questionnaire(Online)` route is local-only until the next WebGL build and upload.
- A stale CPU-bound backend process from the former exhaustive search was force-terminated. Before killing any process in future work, identify the exact stale PID with `ps`; never broadly kill Python processes.
- Backend-only deployment can be done by copying the changed backend files and then running `cd /root/SURF && ./deploy_scp` on the server. The local `deploy_scp.ps1` uploads a broader set including WebGL and frontend assets, so do not use it for a backend-only change without intending that scope.
- Last local verification on 2026-08-02: `python -m unittest discover -s Backend -p "test_*.py"` passed 145 tests. `dotnet build Assembly-CSharp.csproj -v:minimal` completed with 0 errors and 25 warnings from Unity packages/analyzers. The local MatchMaking and Train pages and their static assets returned HTTP 200; interactive browser screenshots were unavailable in the verification environment. A future `--no-restore` build requires the generated `Temp/obj/.../project.assets.json` to exist first.
- The last inspected code baseline before this documentation update was `1611a8f`. Do not treat a recorded commit as authoritative indefinitely; always inspect `git status` and recent history before editing, and preserve unrelated user changes.
- `WebGLBuild/` is generated output and is not committed. For a WebGL update, rebuild with Unity 2022.3.62f2c1, bump the template cache key when stale browser assets are possible, upload `WebGLBuild/`, and verify the remote index plus the data/framework/wasm responses.

## Build, Test, and Development Commands

- `python -m pip install -r Backend/requirements.txt` installs pinned backend dependencies.
- `python Backend/app.py` starts the local service on `http://127.0.0.1:8000`; check `/health`, `/ready`, and `/frontend/`.
- `python -m unittest discover -s Backend -p "test_*.py"` runs all backend unit and API-contract tests.
- Open the project with Unity `2022.3.62f2c1`. Use **File > Build Settings > WebGL > Build** to regenerate `WebGLBuild/`.
- Run Unity tests through **Window > General > Test Runner** when adding EditMode or PlayMode coverage.

## Coding Style & Naming Conventions

Use four-space indentation in C# and Python; keep JavaScript and CSS at their existing four-space style. Follow Unity/C# conventions: `PascalCase` for types, methods, and properties; `camelCase` for fields and locals; and descriptive `*Controller`, `*Client`, or `*Manager` suffixes. Python uses `snake_case`, `PascalCase` test classes, and standard-library imports before third-party imports. No formatter is enforced, so match surrounding code and avoid unrelated reformatting.

## Testing Guidelines

Backend tests use `unittest`, FastAPI `TestClient`, and mocks for model calls. Name files `test_<feature>.py` and methods `test_<behavior>`. Add regression tests for prompt validation, API envelopes, dashboard mutations, and retry behavior. Never require a real API key or network call in tests. For gameplay changes, manually verify affected scenes and the WebGL build path.

## Commit & Pull Request Guidelines

History favors short messages such as `fix` and concise Chinese summaries; use a more specific imperative summary, for example `修复 HA 方案重试计数` or `Add dashboard deletion test`. Keep each commit focused. Pull requests should describe the user-visible change, list tests performed, link related issues, and include screenshots or recordings for scene, dashboard, or WebGL UI changes. Never commit `.env`, API keys, local logs, or generated Unity caches.
