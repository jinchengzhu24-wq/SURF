# Repository Guidelines

## Project Structure

This repository combines a Unity 2D Sokoban client, the 8000 FastAPI service, and the independent 8010 co-creation service. Unity code is under `Assets/Scripts/`; scenes are under `Assets/Scenes/`; preserve every Unity asset's `.meta` file. `Backend/` is the 8000 service, `Frontend/` is its static dashboard, and `CoCreationPrototype/` contains the 8010 backend and frontend. `Library/`, `Temp/`, `Logs/`, generated solution files, and `WebGLBuild/` are generated output.

## Current Product Baseline (2026-08-20)

- The active online route is `Menu -> Questionnaire(Online1) -> Online_Lobby -> Match_Briefing -> DG -> DG_Level -> CoCreation_Entry -> 8010 -> Challenge_Waiting -> Online_Level -> Match_Result -> Questionnaire(Online2) -> Menu`. Both online questionnaires are part of every match cycle: Online1 opens the pre-match WJX survey before the lobby, and Online2 opens the post-match WJX survey after results. Do not add a persistent skip flag.
- `Draft` is retired. PC code, scenes, and backend capability are retained for later work but have no Build Settings entry, menu/navigation route, or current client integration. Do not restore them without a confirmed study-design revision.
- DG asks about opponent relationship and intended experience, then receives an AI summary/difficulty suggestion. Confirmed DG context guides only the 8000 first-draft generator; it must never be sent to, persisted by, or exposed in the 8010 service or its LLM context.
- Menu's `Tutorial` button opens the static bilingual PDF at `http://111.231.136.4/frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf`. It opens in the browser's PDF viewer and does not enter a Unity route.

## Unity Authoring

- When changing a Unity scene, use a connected Unity Editor through MCP. Do not hand-edit scenes, prefabs, or serialized Unity YAML while MCP is available.
- UI must be static, scene-owned, and Inspector-serialized. Do not inject temporary prefabs, create visible controls at runtime, or bind production UI through object-name/path lookup.
- DG keeps world-space grid/camera objects outside `DGCanvas`; all visible DG controls are static children of `DGCanvas`. DG flow changes text and button interactivity, not UI GameObject activation.
- `Online_Level` starts its 30-second opponent challenge timer only after the player gains control. On timeout it locks input, submits a `timed_out` result, and shows the static `ChallengeFailPanel`; `Match_Result` must show timeout and the recorded move count distinctly from completion.

## Research and Co-Creation Guardrails

- `Assets/EssayBase/8-3/SURF_Feedback.pdf`, `Assets/EssayBase/8-3/Feedback_Action_Plan.md`, and the confirmed route in `README.md` are the planning baseline. If a request conflicts with them, identify the conflict and wait for explicit confirmation before implementing; update the relevant planning document with an approved revision.
- Keep researcher-defined goals or conditions out of LLM-visible prompts. The neutral co-creation flow records a designer's self-reported intention only after final Stage confirmation and before opponent results.
- A co-creation session contains one level. `Stage 1`, `Stage 2`, and later names are immutable versions of that level, never separate levels or progression.
- Preserve turns, versions, proposals, decisions, play evidence, timing, final rows, intention, and opponent outcomes. A level is sent to the opponent only after explicit final confirmation.
- The LLM is a warm, first-person design peer. Its interpretations of intention must be tentative and correctable; it must not attribute generator-placed DG tiles to the designer, claim unsaved edits were made, or create/accept a map proposal without explicit authorization and deterministic validation.
- The 8010 workbench begins a persistent 10-minute deadline on first browser access. After expiry, chat/edit/save/restore/play/proposal/language actions are locked; only final Stage submission remains. A valid unsaved local draft may be atomically saved as the final `human_edit` Stage when submitted.

## Online Match and Deployment Rules

- Match room results use `completed` or `timed_out`; legacy results without an outcome are treated as completed. Do not regress idempotency, room identity, or result polling.
- The public endpoints are `http://111.231.136.4/game/`, `/frontend/`, and `/cocreation/`; public links use port 80, not `:8000` or `:8010`.
- For an 8010-only deployment, back up its SQLite database, upload only changed `CoCreationPrototype` files, and restart `sokoban-cocreation`. Never use `/root/SURF/deploy_scp` for an 8010-only change.
- For a WebGL update, rebuild with Unity `2022.3.62f2c1`, bump the WebGL template cache key when browser assets can be stale, upload `WebGLBuild/`, and verify the public index plus loader/data/framework/wasm responses. Uploading `Frontend/tutorial/` alone does not require an 8000 restart.
- Do not commit `.env`, API keys, SQLite databases, research logs, Unity caches, or generated WebGL output. Preserve unrelated user changes in a dirty worktree.

## Build, Test, and Style

- `python Backend/app.py` starts 8000; run `python -m unittest discover -s Backend -p "test_*.py"` for its tests.
- `python CoCreationPrototype/Backend/app.py` starts 8010; run `python -m unittest discover -s CoCreationPrototype/Backend/tests -p "test_*.py"` for its tests.
- Use Unity `2022.3.62f2c1`; run Unity tests through Test Runner as appropriate. Do not build WebGL unless requested.
- Use four-space indentation. C# types/methods are `PascalCase`, C# fields/locals are `camelCase`, and Python uses `snake_case`. Add focused regression coverage for behavior changes; tests must not require a real API key or network call.
