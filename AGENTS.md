# Repository Guidelines

## Project Structure & Module Organization

This repository combines a Unity 2D Sokoban client with a Python service. Unity gameplay code lives in `Assets/Scripts/`, grouped by feature (`Level/`, `LLM/`, `Player/`, `Study/`, and `Menu/`). Scenes are under `Assets/Scenes/`; prefabs, tiles, animations, fonts, and WebGL templates remain in their corresponding `Assets/` folders. Keep every Unity asset paired with its `.meta` file.

`Backend/` contains the FastAPI application, LLM runtime and prompts, plus Python tests. `Frontend/` is the static study dashboard served at `/frontend/`. Unity package and editor settings live in `Packages/` and `ProjectSettings/`. Treat `Library/`, `Temp/`, `Logs/`, generated solution files, and `WebGLBuild/` as generated output.

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
