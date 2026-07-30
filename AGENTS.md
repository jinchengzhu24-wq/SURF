# Repository Guidelines

## Project Structure & Module Organization

This repository combines a Unity 2D Sokoban client with a Python service. Unity gameplay code lives in `Assets/Scripts/`, grouped by feature (`Level/`, `LLM/`, `Player/`, `Study/`, and `Menu/`). Scenes are under `Assets/Scenes/`; prefabs, tiles, animations, fonts, and WebGL templates remain in their corresponding `Assets/` folders. Keep every Unity asset paired with its `.meta` file.

`Backend/` contains the FastAPI application, LLM runtime and prompts, plus Python tests. `Frontend/` is the static study dashboard served at `/frontend/`. Unity package and editor settings live in `Packages/` and `ProjectSettings/`. Treat `Library/`, `Temp/`, `Logs/`, generated solution files, and `WebGLBuild/` as generated output.

## Current Project Status (2026-07-30)

The active matchmaking route is:

```text
Menu -> Competition_Mode -> AI_Asistant_Mode -> PC -> PC_Design -> PC_Level
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
- The public request remains `width`, `height`, `sketchRows`, optional retry context, and `maxAttempts`. The public response remains `{"rows":[...10 rows...]}`.
- The backend normalizes the request once, enumerates legal `2x2` through `4x4` water rectangles, and checks at most six water choices.
- For each checked water choice, the backend solves the no-new-wall map once and records a complete solution trace. Generated walls are selected only from cells never occupied by the player or boxes in that trace, so the recorded solution remains valid after the walls are added.
- Cheap greedy scoring constructs at most six complete layouts. Five internal walls are preferred, four are retained as an intermediate fallback, and the guaranteed minimum is three internal walls plus one water area. Bent, split, or dispersed wall shapes rank above a straight line.
- Generated walls may not cover fixed user tiles, touch a box start, divide the activity area, sit orthogonally next to an outer-shell wall, appear directly above water, or contain a complete `2x2` block made only from generated wall cells. User-authored wall blocks are not subject to the generated-wall `2x2` restriction. There is currently no 48-cell post-generation activity-area minimum; the PC_Design submission minimum remains 56.
- The LLM receives complete layouts and returns only `{"layoutCandidateId": n}`. It cannot mix player, water, or wall IDs from different candidates.
- PC generation makes one model call with a 15-second backend timeout. Invalid JSON, an unknown ID, timeout, connection failure, or other `LLMServiceError` immediately selects the highest-ranked safe candidate instead of making a second model call.
- Do not restore the deleted wall-combination enumeration, counterfactual wall-impact search, or the requirement that walls increase shortest steps or pushes. Stability and guaranteed solvability are the current priority.
- Unity still validates the full rows and runs `LevelSolver` with `maxSearchStates: 300000` before loading. Its minimum generated feature check is three new wall tiles and one rectangular water area.
- Relevant files are `Backend/app.py` (`normalize_pc_level_request`, `build_pc_safe_layout_candidates`, and `create_pc_level_candidate`), `Backend/prompt.py`, `Assets/Scripts/LLM/PCLevelExpansionClient.cs`, `Assets/Scripts/Level/PCLevelExpansionController.cs`, and `PCLevelCandidateValidator.cs`.

### Deployment and verification state

- `Backend/app.py` and `Backend/prompt.py` were synchronized to `/root/SURF/Backend/` on `111.231.136.4` and the backend was restarted. `/ready` returned ready.
- A real server request completed in about 0.8-1.2 seconds, used one model attempt, and returned a `12x10` result with four new walls, one `2x2` water area, and one player. The sample preprocessing log reported about 169 ms, 405 searched states, and six complete candidates.
- A stale CPU-bound backend process from the former exhaustive search was force-terminated. Before killing any process in future work, identify the exact stale PID with `ps`; never broadly kill Python processes.
- Backend-only deployment can be done by copying the changed backend files and then running `cd /root/SURF && ./deploy_scp` on the server. The local `deploy_scp.ps1` uploads a broader set including WebGL and frontend assets, so do not use it for a backend-only change without intending that scope.
- Unity source changes are present locally but were not rebuilt or uploaded as a new WebGL build during this update.
- Last verification: `python -m unittest discover -s Backend -p "test_*.py"` passed 107 tests. `dotnet build Assembly-CSharp.csproj --no-restore -v:minimal` completed with 0 errors; remaining warnings came from Unity packages.
- The current PC implementation is committed at `f5a90a3` (`以可生成为优先`). Always inspect `git status` for newer user work before making further changes. That implementation touched the scene/client timeout, PC design validator and sketch controller, backend app and prompt, and PC backend tests.
- The PC description in `README.md` still mentions direct coordinate selection and is stale. The complete-candidate-ID protocol described here and implemented in code is authoritative until that README section is updated.

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
