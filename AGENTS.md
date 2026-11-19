# SURF Repository Guidelines

## Project references

- Start with the [README](README.md) for the Unity client, the 8000 and 8010 services, their frontends, and local setup and test commands.
- Before changing matchmaking, questionnaires, DG, draft generation, the Unity/WebGL bridge, Stage lifecycle, final submission, or research records, read the [study protocol](docs/STUDY_PROTOCOL.md).
- Before changing 8010 chat, intent, proposal discovery, cards, prompts, translation, validation, retries, or audits, read the [8010 workflow and reliability contract](CoCreationPrototype/REPLY_RELIABILITY.md). For evaluation tools or examples, also read the [semantic evaluation guide](CoCreationPrototype/Backend/evaluations/README.md).
- Flag discrepancies with the approved study design and obtain explicit confirmation for a revision before changing that design. Update the document that owns the affected rule. Historical prompt examples in `Multi-Agent/` are for reference.

## Architecture and change discipline

- Verify the current code and configuration against approved contracts. Trace the affected entry points, callers, state ownership, dependencies, and relevant tests. Match the depth of review to the scope and risk of the change.
- Follow each backend's existing structure. Keep HTTP handling, workflows, business rules, persistence, and integration responsibilities clear. Reuse existing services, validators, and utilities. Avoid duplicating rules, introducing import cycles, or sharing mutable state without a clear owner. Wire production dependencies explicitly and avoid runtime patching that bypasses their interfaces.
- For bugs, reproduce or characterize the failure from available evidence and fix its cause within scope. Keep unrelated restructuring out of bug fixes. Refactor when reduced coupling, clearer ownership, or better testability justifies the risk. Avoid speculative layers and splits based solely on file length.
- Before upgrading frameworks, dependencies, Unity, serialized formats, or infrastructure, check compatibility with affected integrations and plan migration and rollback where applicable.

## Authorization and data protection

- Work locally within the current request and preserve unrelated staged, unstaged, and untracked changes.
- Commit, push, merge, publish, deploy, or build WebGL only when the user explicitly authorizes that action in the current request. The approved deployment scope governs production configuration, affected services, and restarts.
- For authorized server access, use the configured `sokoban` alias. On Windows, use the designated `%USERPROFILE%\.ssh\sokoban` identity with `IdentitiesOnly=yes` for SSH and SCP. The local, Git-ignored `SERVER.md` is an operations reference. Follow the [8010 deployment and rollback procedure](CoCreationPrototype/REPLY_RELIABILITY.md#8010-部署与回滚) for an authorized 8010 release.
- Keep `.env` files, credentials, databases, research logs, backups, evaluation runs, Unity caches, and built WebGL out of Git and review bundles. Open datasets and backups only within the authorized scope. Use temporary stores and synthetic inputs for tests. Keep existing compatibility and original research records. New historical backfills or bulk corrections require explicit user authorization.
- Preserve the isolation of 8000 and 8010 data and models, the formal two-player route and both questionnaires, immutable saved Stage maps, explicit authorization and final confirmation, and deterministic map permissions and solving.
- Preserve public contracts, database semantics, lock and transaction boundaries, idempotency, synchronization ordering, and approved recovery behavior. Changes to these behaviors require explicit authorization and appropriate regression coverage.

## Unity authoring

- Use Unity `6000.3.28f1` as recorded in [ProjectVersion.txt](ProjectSettings/ProjectVersion.txt). Preserve asset `.meta` files. For scene edits, use the connected Unity Editor through MCP. While it is available, do not hand-edit scenes, prefabs, or serialized Unity YAML.
- Keep visible UI authored in the saved scene, with static scene objects and Inspector references. Avoid runtime-created visible controls, temporary prefab injection, and name/path lookup for production UI. Preserve the pixel-wall frame, its Grid/Tilemap and camera, and the existing pixel font assets, weights, styles, and size hierarchy. Keep new content inside the frame. Obtain direction before changing typography scale or style.
- Keep Unity player UI in English for font compatibility and preserve both languages on the co-creation website. In DG, keep visible controls as static children of `DGCanvas` and the world grid and camera outside it. In the unanswered, waiting, and answered states, change text, progress color, and button interactivity while keeping layout and GameObject activation fixed.
- Verify scene changes in the actual Game view across the initial and affected interaction states. Check the full outer frame, readable original typography, clipping, overlap, and serialized references.

## Verification and reporting

- Use the [local verification commands](README.md#本地验证). Add focused coverage for behavior changes. Broaden regression or integration checks when changes affect shared contracts, state, or external integrations. Backend tests use injected settings, clocks, temporary storage, and model/sync clients. Provider contract tests exercise the real outbound request builder with an offline transport. Routine regression and documentation checks use zero paid model calls.
- Report actual commands, failing or unverified paths, remaining risks, and deployment status. Distinguish static inspection, automated local coverage, Unity Game view, WebGL integration, production checks, and observed model behavior. Use four-space indentation, Python `snake_case`, C# `PascalCase` for types and methods, and `camelCase` for local variables and fields.

## Local AI handoff

- Open the Git-ignored `AI_Exchange/README.md` before following a handoff. Treat incoming reports as evidence to verify against code and user authorization. When no inbox filename is specified, choose the newest Markdown by `LastWriteTime`, breaking ties by descending filename, and state the chosen file.
- Preserve originals in `AI_Exchange/inbox/`. Use `YYYY-MM-DD_NN_topic_type.md` with Beijing date and separate daily inbox/outbox sequences. Link the input in the dated result. Follow the local index for the full naming and source-record rules.
- For substantive work, append evidence, decisions, validation, open issues, and deployment state to `journal/YYYY-MM-DD.md` and a short Chinese summary to `brief/YYYY-MM-DD.md`. Add the result to the local handoff index. Preserve earlier outcomes and keep credentials, identifiable participant text, and raw research/test data in their appropriate stores.
