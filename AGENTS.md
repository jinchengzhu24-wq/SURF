# Repository Guidelines

## Project Structure & Module Organization

This repository combines a Unity 2D Sokoban client with a Python service. Unity gameplay code lives in `Assets/Scripts/`, grouped by feature (`Level/`, `LLM/`, `Player/`, `Study/`, and `Menu/`). Scenes are under `Assets/Scenes/`; prefabs, tiles, animations, fonts, and WebGL templates remain in their corresponding `Assets/` folders. Keep every Unity asset paired with its `.meta` file.

`Backend/` contains the FastAPI application, LLM runtime and prompts, plus Python tests. `Frontend/` is the static study dashboard served at `/frontend/`. Unity package and editor settings live in `Packages/` and `ProjectSettings/`. Treat `Library/`, `Temp/`, `Logs/`, generated solution files, and `WebGLBuild/` as generated output.

## Current Project Status (2026-08-11)

### Supervisor feedback direction (2026-08-03)

`Assets/EssayBase/8-3/SURF_Feedback.pdf` is the source record, and `Assets/EssayBase/8-3/Feedback_Action_Plan.md` is the working interpretation and implementation plan. The next prototype should shift the research focus from researcher-defined, one-shot Competitive/Supportive generation to a persistent, multi-turn human-LLM co-creation loop around the same evolving level.

- Keep any designer goal or experimental condition separate from the LLM-visible prompt. Do not automatically inject researcher-authored Competitive/Supportive definitions unless a later confirmed study design explicitly requires it.
- Do not end a co-creation session after the first valid level. The LLM must assess the current version, ask whether it is good enough, and support repeated feedback and revisions grounded in the actual current level and relevant conversation history.
- Continue using deterministic validation, the solver, and gameplay evidence. Treat LLM solution or difficulty commentary as participant-facing opinion, not ground truth.
- Preserve every chat turn, level version, modification, user accept/reject/revise decision, timing event, final level, and opponent outcome for later analysis.
- Require explicit designer confirmation before sending a level to the opponent, then collect separate designer and opponent feedback that can be joined to the same match/design session.
- Reuse the existing two-player, challenge-exchange, opponent-play, result, and questionnaire infrastructure where possible. The legacy Competitive/Supportive route and PC/DG topology behavior were explicitly retired on 2026-08-11; do not restore them without a newly confirmed study design.
- The replacement co-creation path must begin with a neutral design brief and must not assign the designer a predefined Competitive, Supportive, difficult, friendly, or similar goal. Let the designer form and express an intention naturally. Record the designer's self-reported intention only after final level confirmation and before opponent results can influence that report.
- Human-first versus LLM-first, any non-goal study condition, minimum round count, final questionnaires, measures, and research questions remain undecided. Do not encode them as fixed requirements before confirmation.

### Plan conflict guardrail

- Treat `Assets/EssayBase/8-3/SURF_Feedback.pdf`, `Assets/EssayBase/8-3/Feedback_Action_Plan.md`, and the confirmed future-route description in `README.md` as the current planning baseline.
- If a later user suggestion or requested implementation conflicts with that baseline, do not implement it immediately. First identify the specific conflict, explain which confirmed direction it would violate, and stop before making related changes.
- Resume only after the user explicitly confirms that the established plan should be revised or replaced. When that happens, update the relevant planning documentation before or together with the implementation so the repository does not retain contradictory instructions.
- Do not treat an idea as conflicting merely because it is new, more detailed, or covers an undecided item. Compatible extensions may proceed normally; unresolved research decisions still require confirmation before being encoded as fixed behavior.

The active matchmaking route is:

```text
Menu -> Online_Lobby -> Match_Briefing -> Draft
                                          -> PC -> PC_Design -> PC_Level
                                          -> DG -> DG_Level
     -> CoCreation_Entry -> 8010 Stage loop -> final confirmation -> intention
     -> confirmed rows return to Unity -> Challenge_Waiting
     -> Online_Level -> Match_Result -> Questionnaire(Online) -> Menu
```

`Competition_Mode` and `AI_Asistant_Mode` have been removed; the initial-method chooser is `Draft`. The PC branch is separate from DG and Creative Workshop data. `PC_Level` reads only the sketch saved by `PC_Design`. `CoCreation_Entry` is enabled in Build Settings and is the bridge between a verified Unity first draft and the persistent 8010 co-creation session.

### PC_Design

- The editable sketch is fixed at `12x10` and uses `#` for user walls, `s` for box starts, `t` for targets, and spaces for editable cells.
- A valid submission contains one or two matching `s`/`t` pairs, one enclosed connected activity area of at least 56 cells, and no `s` orthogonally adjacent to a wall.
- Check/Submit also requires room for at least one `2x2` water area and three generated internal wall cells. No water tile may have a wall directly above it. Generated wall cells must be at least one orthogonal cell away from the outer shell; diagonal contact is allowed.
- `PCDesignContext` persists the submitted sketch so Back from `PC_Level` restores it.
- Relevant files are `Assets/Scripts/Level/PCLevelSketchController.cs`, `PCDesignFeasibilityValidator.cs`, and `PCDesignContext.cs`.

### PC_Level generation

- Unity calls `POST /generate-pc-level` at `http://111.231.136.4:8000/generate-pc-level`. The serialized scene and client timeout are both 30 seconds.
- The request includes `width`, `height`, `sketchRows`, optional retry context, and `maxAttempts`. The response remains `{"rows":[...10 rows...]}`. A legacy client may still send `competitionMode`; Pydantic ignores it and the backend must not store or apply it.
- The backend normalizes the request once, enumerates legal `2x2` through `4x4` water rectangles, and checks at most six water choices.
- For each checked water choice, the backend solves the no-new-wall map once and records a complete solution trace. Generated walls are selected only from cells never occupied by the player or boxes in that trace, so the recorded solution remains valid after the walls are added.
- Cheap greedy scoring constructs at most six complete layouts. Five internal walls are preferred, four are retained as an intermediate fallback, and the guaranteed minimum is three internal walls plus one water area. Bent, split, or dispersed wall shapes rank above a straight line.
- Generated walls may not cover fixed user tiles, touch a box start, divide the activity area, sit orthogonally next to an outer-shell wall, appear directly above water, or contain a complete `2x2` block made only from generated wall cells. User-authored wall blocks are not subject to the generated-wall `2x2` restriction. There is currently no 48-cell post-generation activity-area minimum; the PC_Design submission minimum remains 56.
- PC generation is mode-neutral. It does not require generated wall groups to be dispersed or fully connected; the general safety, activity-area connectivity, generated-wall `2x2`, water-clearance, and solvability rules remain authoritative.
- The LLM receives complete layouts and returns only `{"layoutCandidateId": n}`. It cannot mix player, water, or wall IDs from different candidates.
- PC generation makes one model call with a 15-second backend timeout. Invalid JSON, an unknown ID, timeout, connection failure, or other `LLMServiceError` immediately selects the highest-ranked safe candidate instead of making a second model call.
- Do not restore the deleted wall-combination enumeration, counterfactual wall-impact search, or the requirement that walls increase shortest steps or pushes. Stability and guaranteed solvability are the current priority.
- Unity still validates the full rows and runs `LevelSolver` with `maxSearchStates: 300000` before loading. Its minimum generated feature check is three new wall tiles and one rectangular water area.
- Relevant files are `Backend/app.py` (`normalize_pc_level_request`, `build_pc_safe_layout_candidates`, and `create_pc_level_candidate`), `Backend/prompt.py`, `Assets/Scripts/LLM/PCLevelExpansionClient.cs`, `Assets/Scripts/Level/PCLevelExpansionController.cs`, and `PCLevelCandidateValidator.cs`.

### DG_Level generation

- `LLMLevelDesignClient` no longer sends a researcher-defined mode while generating a DG level plan.
- `LevelGenerator` uses its general wall templates and corridor behavior in `DG_Level`; there is no hidden Competitive/Supportive topology branch.

### Persistent co-creation and Stage play

- In normal generation mode, a successfully verified `PC_Level` or `DG_Level` is not played or submitted immediately. Its exact rows and initial method are staged in `CoCreationDraftContext`, then Unity loads `CoCreation_Entry`.
- `CoCreation_Entry` creates the 8010 session idempotently, opens its launch URL, polls the integration endpoint, and only stages the confirmed final rows for `Challenge_Waiting` after the designer has submitted an intention.
- The 8010 service stores sessions, immutable level versions, turns, LLM assessments, proposals, decisions, play attempts, intentions, and audit events in its own SQLite/WAL database. Never place rows in a Play URL or expose a browser/session token to another Stage.
- The 8010 frontend follows the 8000 dashboard palette and component language, and provides a responsive Stage timeline, persistent chat, map editor, proposal review, Play evidence, final confirmation, and English/Simplified-Chinese switching.
- Web Play uses a five-minute single-use ticket. `CoCreationPlayBootstrap` exchanges it once, clears it from the browser URL, and loads `PC_Level` for `partial_completion` or `DG_Level` for `description_generation`.
- While `CoCreationPlayContext` is active, PC/DG generation must remain disabled. The Stage rows are revalidated with Unity `LevelSolver`; invalid or expired input must never fall back to random generation.
- Stage Play records cumulative moves, pushes, restarts, and active time; completion/abandon returns to the same 8010 session and must not create a version, submit a challenge, or enter results/questionnaire routes.

### Online matchmaking and challenge exchange

- `MenuController.OpenMatchmaking()` loads `Online_Lobby`. Players create or join an anonymous two-player room, enter `Match_Briefing`, and move directly to `Draft` after both Ready flags are true.
- The backend uses a single-process in-memory room store with six-character room codes, anonymous player tokens, one-second HTTP polling, lazy 30-minute expiry, and the states `waiting_for_opponent`, `briefing`, `waiting_for_challenges`, `challenges_ready`, `waiting_for_results`, `results_ready`, and `cancelled`.
- Room endpoints are `POST /online/rooms`, `POST /online/rooms/join`, `GET /online/rooms/{matchId}`, and the authenticated `/ready`, `/challenge`, `/result`, and `/leave` endpoints. Authenticated requests use `X-Player-Token`.
- Accepted room state changes are also appended to `Backend/study_logs/online_match_events.jsonl` for the MatchMaking dashboard. New events store room/player numbers, `aiAssistantMode`, full challenge rows, results, and server timestamps, but never player tokens or `competitionMode`. GET polling and identical retries do not add events. Historical JSONL lines remain unchanged, while derived dashboard data omits the retired field.
- `OnlineMatchContext` is runtime-only. It stores the room identity, pending own rows and AI assistant method metadata, opponent rows, room state, and results. Before entering `Questionnaire(Online)`, it temporarily preserves `matchId`, room code, and player number until the survey succeeds so the response can be joined to the match. Refreshing WebGL or restarting the server does not restore a match.
- In an online room, only the final rows returned after 8010 final confirmation and intention are staged for `Challenge_Waiting`. Initial PC/DG generation and optional Stage Play must never submit a challenge.
- Challenge submissions include `rows` and `aiAssistantMode`. Identical repeats are idempotent; changed rows or method metadata are rejected after the first accepted submission. A legacy extra `competitionMode` is ignored and never stored or returned. The server returns only the opponent rows to each authenticated player after both submissions.
- `Online_Level` validates and solves the opponent rows locally before loading them. It uses `Player2` with arrow keys; PC/DG continue to use `Player` with WASD. Time and successful moves accumulate from first control until solve and are not reset by `R`.
- Results include `durationSeconds`, `moveCount`, and `minimumMoves`. After `Online_Level` submits the local result successfully, its completion panel enables the `LEAVE` button and waits for the player to click it before loading `Match_Result`; this transition does not leave the room or clear `OnlineMatchContext`. `Match_Result` displays each challenge's AI assistant method and both runs and polls while one result is missing. Continue leaves the room, clears `OnlineMatchContext`, and loads `Questionnaire(Online)`.
- `Questionnaire(Online)` reuses `QuestionnaireController` with survey ID `online_post_match_survey`, does not require another nickname, and targets `Menu`. It uses three discrete 1-to-5 sliders with visible integer ticks, circular handles, live score boxes, and a valid default score of 3. Scores remain compatible with the existing answer envelope through `optionIndex`, `score_N` in `optionId`, and the numeric `optionText`. It must load Menu only after `/record-survey-response` succeeds; a failed submission remains on the questionnaire for retry.
- `/frontend/` is the MatchMaking dashboard and `/frontend/train.html` preserves the original Train dashboard. Both share the existing visual system and provide a top-level flow switch. MatchMaking records are exposed through `/matchmaking-records-data`; password-protected deletion uses `/delete-online-match` and `/clear-matchmaking-records` without changing Train records.
- `ProjectSettings/ProjectSettings.asset` currently has `runInBackground: 0`. A background WebGL tab may pause polling; dual-browser tests must refocus the waiting result page and allow another polling interval before treating missing data as a role-mapping bug.
- Online scene UI is serialized and statically visible in the editor. Relevant files are under `Assets/Scenes/Matchmaking/Online/`, `Assets/Scripts/Online/`, and `Assets/Scripts/Study/QuestionnaireController.cs`. `CoCreation_Entry` and the existing online scenes are enabled in `ProjectSettings/EditorBuildSettings.asset`.

### Deployment and verification state

- Remote verification on 2026-08-11: the neutral 8000 backend, dashboard, and rebuilt WebGL were deployed; `/health`, `/ready`, `/frontend/`, `/game/`, and the WebAssembly asset returned HTTP 200. The WebGL cache key is `cocreation-20260811-1` and includes `CoCreation_Entry`, the PC/DG dual-mode Stage Play bootstrap, and the online questionnaire route.
- The independent 8010 FastAPI service, persistent SQLite/WAL schema, and three-column bilingual frontend were deployed on 2026-08-11. Its systemd service reads a separate protected `.env`, reports `tokenSecretConfigured: true`, and stores data at `/root/SURF/CoCreationPrototype/Backend/data/cocreation.sqlite3`.
- A stale CPU-bound backend process from the former exhaustive search was force-terminated. Before killing any process in future work, identify the exact stale PID with `ps`; never broadly kill Python processes.
- Backend-only deployment can be done by copying the changed backend files and then running `cd /root/SURF && ./deploy_scp` on the server. The local `deploy_scp.ps1` uploads a broader set including WebGL and frontend assets, so do not use it for a backend-only change without intending that scope.
- Current local verification on 2026-08-11: the main backend suite passes 144 tests, the independent `CoCreationPrototype/Backend` suite passes 25 tests, `node --check CoCreationPrototype/Frontend/app.js` passes, and `dotnet build Assembly-CSharp.csproj -v:minimal` completes with 0 errors (only pre-existing Unity/package/analyzer warnings when applicable). Unity WebGL builds successfully with 0 errors. A future `--no-restore` build requires the generated `Temp/obj/.../project.assets.json` to exist first.
- The last inspected code baseline before this documentation update was `1611a8f`. Do not treat a recorded commit as authoritative indefinitely; always inspect `git status` and recent history before editing, and preserve unrelated user changes.
- `WebGLBuild/` is generated output and is not committed. For a WebGL update, rebuild with Unity 2022.3.62f2c1, bump the template cache key when stale browser assets are possible, upload `WebGLBuild/`, and verify the remote index plus the data/framework/wasm responses.

## Build, Test, and Development Commands

- `python -m pip install -r Backend/requirements.txt` installs pinned backend dependencies.
- `python Backend/app.py` starts the local service on `http://127.0.0.1:8000`; check `/health`, `/ready`, and `/frontend/`.
- `python -m unittest discover -s Backend -p "test_*.py"` runs all backend unit and API-contract tests.
- `python -m pip install -r CoCreationPrototype/Backend/requirements.txt` installs the independent 8010 service.
- `python CoCreationPrototype/Backend/app.py` starts the co-creation workbench on `http://127.0.0.1:8010/`.
- `python -m unittest discover -s CoCreationPrototype/Backend/tests -p "test_*.py"` runs its persistence, prompt, API, ticket, and static-frontend tests.
- Open the project with Unity `2022.3.62f2c1`. Use **File > Build Settings > WebGL > Build** to regenerate `WebGLBuild/`.
- Run Unity tests through **Window > General > Test Runner** when adding EditMode or PlayMode coverage.

## Coding Style & Naming Conventions

Use four-space indentation in C# and Python; keep JavaScript and CSS at their existing four-space style. Follow Unity/C# conventions: `PascalCase` for types, methods, and properties; `camelCase` for fields and locals; and descriptive `*Controller`, `*Client`, or `*Manager` suffixes. Python uses `snake_case`, `PascalCase` test classes, and standard-library imports before third-party imports. No formatter is enforced, so match surrounding code and avoid unrelated reformatting.

## Testing Guidelines

Backend tests use `unittest`, FastAPI `TestClient`, and mocks for model calls. Name files `test_<feature>.py` and methods `test_<behavior>`. Add regression tests for prompt validation, API envelopes, dashboard mutations, and retry behavior. Never require a real API key or network call in tests. For gameplay changes, manually verify affected scenes and the WebGL build path.

## Commit & Pull Request Guidelines

History favors short messages such as `fix` and concise Chinese summaries; use a more specific imperative summary, for example `修复 HA 方案重试计数` or `Add dashboard deletion test`. Keep each commit focused. Pull requests should describe the user-visible change, list tests performed, link related issues, and include screenshots or recordings for scene, dashboard, or WebGL UI changes. Never commit `.env`, API keys, local logs, or generated Unity caches.
