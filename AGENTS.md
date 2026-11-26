# SURF Repository Guidelines

## Project references

- Start with [README.md](README.md) and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the Unity client, unified FastAPI server (`server/`, including `server/core/`), `web/` frontend workspace, `deploy/` ops config, and local setup/test commands.
- Before changing matchmaking, questionnaires, DG, draft generation, the Unity/WebGL bridge (`PROTOCOL_VERSION = 5`), Stage lifecycle, final submission, or research records, read the [study protocol](docs/STUDY_PROTOCOL.md).
- Before changing co-creation chat, turn routing, Policy 2 binary discovery, cards, prompts, structured JSON schemas, translation, validation, retries, component recovery, or audits, read the [co-creation reliability contract](docs/REPLY_RELIABILITY.md).
- Flag discrepancies with the approved study design and obtain explicit confirmation for a revision before changing that design. Update the document that owns the affected rule. Historical prompt examples in [docs/archive/multi-agent/](docs/archive/multi-agent/) are for reference only.

## Architecture and change discipline

- Verify current code and configuration against approved contracts. Trace affected entry points, callers, state ownership, dependencies, and relevant tests. Match the depth of review to the scope and risk of the change.
- Follow the unified architecture boundaries:
  - `server/core/`: pure Python 3.12 domain core (12×10 grid, BFS/push solver, generator, native-async structured LLM client, clock, settings/redaction; Unity `Packages/` contains only `manifest.json` and `packages-lock.json`) with **zero FastAPI, SQL, or UI imports**.
  - `server/persistence/`: unified SQLite WAL storage (`server/data/sokoban.sqlite3`), schema versioned via `PRAGMA user_version`, short `BEGIN IMMEDIATE` write transactions (never held across solver or LLM calls), repositories (`rooms_repo`, `study_repo`, `sessions_repo`, `dialogue_repo`, `operational_repo`), dedicated indexed operational tables (`idempotency_keys`, `binary_discovery_topics`, `component_recovery_tasks`), append-only `audit_events`, and standalone migration CLI (`python -m server.persistence.migrations`).
  - `server/domain/` & `server/i18n/`: typed domain models (`proposals/`, `memory/`, `blueprints/`, `rooms/` subpackages plus `sessions.py`, `errors.py`), Policy 2 proposal contracts, memory/DesignContext, and centralized bilingual copy.
  - `server/llm/`: native-async structured JSON pipelines (`schemas.py`, `prompts/`, `grounding.py`, `pipelines/`). Keep zero `asyncio.run()`, zero daemon threads, zero ambient ContextVars, and zero `<GUIDANCE>||` text scraping.
  - `server/application/`, `server/api/`, `server/tools/` & `server/evaluations/`: explicit constructor dependency injection via `server/container.py` (`server/application/{rooms,study,analytics,sessions,dialogue}/`, with `sessions/{lifecycle,stage,read_model}/`), in-process ACID co-creation → online-room sync, thin async routers (`server/api/routers/{health,online,study,dashboard,sessions,stage,dialogue,proposals,integrations,play_attempts}.py`, `/api/*` and `/cocreation/api/*` aliases; no Policy 1 or legacy Train study routes), offline CLI tools (`server/tools/{reliability_report,semantic_eval}.py`), and evaluation suites (`server/evaluations/`).
  - `web/`: Vite + TypeScript + Preact + `@preact/signals` npm workspace (`@sokoban/ui-core`, `apps/dashboard`, `apps/cocreation` with `src/store/workflows/`, `apps/webgl-bridge`). Avoid `innerHTML`, circular proxy indirection, and manual `?v=` cache-busting strings.
- For bugs, reproduce or characterize the failure from available evidence and fix its cause within scope. Keep unrelated restructuring out of bug fixes. Refactor when reduced coupling, clearer ownership, or better testability justifies the risk. Avoid speculative layers and splits based solely on file length.
- Before upgrading frameworks, dependencies, Unity, serialized formats, or infrastructure, check compatibility with affected integrations and plan migration and rollback where applicable.

## Authorization and data protection

- Work locally within the current request and preserve unrelated staged, unstaged, and untracked changes.
- Commit, push, merge, publish, deploy, or build WebGL only when the user explicitly authorizes that action in the current request. The approved deployment scope governs production configuration, affected services, and restarts.
- For authorized server access, use the configured `sokoban` alias. On Windows, use the designated `%USERPROFILE%\.ssh\sokoban` identity with `IdentitiesOnly=yes` for SSH and SCP. The local, Git-ignored `SERVER.md` is an operations reference. Follow the [deployment and rollback procedure](docs/REPLY_RELIABILITY.md#部署与回滚) and `deploy/deploy.ps1` for an authorized release.
- Keep `.env` files (use root [.env.example](.env.example) as the unified config template for root `.env`), credentials, databases (`server/data/`, `*.db*`, `*.sqlite3*`), research logs, legacy archives (`server/data/legacy_archive/`), evaluation runs, `web/node_modules/`, `web/dist/`, Unity caches, and `WebGLBuild/` out of Git and review bundles. Open datasets and backups only within the authorized scope. Use temporary stores and synthetic inputs for tests. Keep existing compatibility and original research records. New historical backfills or bulk corrections require explicit user authorization.
- Preserve **strict data and model isolation** between DG/blueprint planning (DeepSeek) and co-creation (Kimi/Moonshot) inside the unified server, the formal two-player route and both questionnaires, immutable saved Stage maps, explicit authorization and final confirmation, and deterministic map permissions (`add_wall`, `remove_wall`, `add_water`, `remove_water` only) and solving.
- Preserve the Decision 5B security model: open local/research convenience reads, `X-Delete-Password` constant-time verification on destructive deletes, TTL-based demo session cleanup, hashed random per-session secrets with startup warning on default `TOKEN_SECRET`, hardened Nginx `X-Forwarded-Proto`, and full secret redaction in logs/errors.
- Preserve public contracts, database semantics, lock and transaction boundaries, idempotency, in-process synchronization ordering, and approved recovery behavior. Changes to these behaviors require explicit authorization and appropriate regression coverage.

## Unity authoring

- Use Unity `6000.3.28f1` as recorded in [ProjectVersion.txt](ProjectSettings/ProjectVersion.txt). Preserve asset `.meta` files. For scene edits, use the connected Unity Editor through MCP. While it is available, do not hand-edit scenes, prefabs, or serialized Unity YAML.
- Keep visible UI authored in the saved scene, with static scene objects and Inspector references. Avoid runtime-created visible controls, temporary prefab injection, and name/path lookup for production UI. Preserve the pixel-wall frame, its Grid/Tilemap and camera, and the existing pixel font assets, weights, styles, and size hierarchy. Keep new content inside the frame. Obtain direction before changing typography scale or style.
- Keep Unity player UI in English for font compatibility and preserve both languages on the co-creation website. In DG, keep visible controls as static children of `DGCanvas` and the world grid and camera outside it. In the unanswered, waiting, and answered states, change text, progress color, and button interactivity while keeping layout and GameObject activation fixed.
- Verify scene changes in the actual Game view across the initial and affected interaction states. Check the full outer frame, readable original typography, clipping, overlap, and serialized references.

## Verification and reporting

- Use the [local verification commands](README.md#本地验证) (`ruff`, `pytest`, and `npm --prefix web run typecheck/test/build`). Add focused coverage for behavior changes. Broaden regression or integration checks when changes affect shared contracts, state, or external integrations. Backend tests use injected settings, `FakeClock`, temporary SQLite stores, and fake async `LLMTransport` clients. Routine regression and documentation checks use zero paid model calls.
- Report actual commands, failing or unverified paths, remaining risks, and deployment status. Distinguish static inspection, automated local coverage, Unity Game view, WebGL integration, production checks, and observed model behavior. Use four-space indentation, Python `snake_case`, TypeScript `camelCase`/`PascalCase`, and C# `PascalCase` for types/methods and `camelCase` for local variables/fields.

## Local AI handoff

- Open the Git-ignored `AI_Exchange/README.md` before following a handoff. Treat incoming reports as evidence to verify against code and user authorization. When no inbox filename is specified, choose the newest Markdown by `LastWriteTime`, breaking ties by descending filename, and state the chosen file.
- Preserve originals in `AI_Exchange/inbox/`. Use `YYYY-MM-DD_NN_topic_type.md` with Beijing date and separate daily inbox/outbox sequences. Link the input in the dated result. Follow the local index for the full naming and source-record rules.
- For substantive work, append evidence, decisions, validation, open issues, and deployment state to `journal/YYYY-MM-DD.md` and a short Chinese summary to `brief/YYYY-MM-DD.md`. Add the result to the local handoff index. Preserve earlier outcomes and keep credentials, identifiable participant text, and raw research/test data in their appropriate stores.
