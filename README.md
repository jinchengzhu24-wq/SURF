# SURF · Sokoban 人机共创与在线挑战

SURF 是一个 Unity 2D 推箱子研究项目。参与者通过设计问题（DG）和 AI 首版规划得到一张地图，在网页共创工作台与 AI 协同修改，再用定稿地图进行双人在线挑战。

## 系统组成

| 组件 | 职责 |
| --- | --- |
| Unity / WebGL | 赛前/赛后问卷、在线大厅与匹配、DG 交互、首版生成与验证、Stage 试玩、双人挑战；WebGL 模板内置 `sokoban-bridge.js`（协议版本 5）。 |
| 统一后端（`:8000`） | 单一 Python 3.12 FastAPI 服务（`server/`，含纯核心 `server/core/`），承载在线匹配、DG 指导与蓝图规划、研究记录与 Dashboard API、共创工作台 API（`/api/*` 及 `/cocreation/api/*` 别名）、进程内 ACID 同步与统一 SQLite WAL 存储。 |
| 前端工作区（`web/`） | Vite + TypeScript + Preact + `@preact/signals` npm workspace，共享 `@sokoban/ui-core`，构建 Matchmaking Dashboard（`/frontend/`）、双语共创工作台（`/cocreation/`，状态工作流位于 `src/store/workflows/`）及 WebGL 桥接脚本。 |

虽然服务已合并为单进程，**DG/蓝图模型与共创模型仍保持严格隔离**：DG 首版理解与关卡蓝图规划仅使用 DeepSeek 配置；共创对话、Policy 2 二选一发现、关卡修改规划与翻译仅使用 Kimi/Moonshot 配置。生成器、地图权限校验（AI 仅可编辑内部墙与水域）和求解由 `server/core/` 中的确定性程序执行。完整分层与数据流见[系统架构](docs/ARCHITECTURE.md)。

## 当前在线流程

```text
Unity 赛前问卷 → 大厅与匹配 → DG 与首版生成
  → /cocreation/ iframe 预览 Draft → 进入共创并固化 Stage 1
  → 聊天、编辑、Policy 2 提案与试玩 → 确认最终 Stage → 提交设计意图
  → 进程内 ACID 同步至在线房间 → Unity 等待对手 → 挑战 → 结果 → 赛后问卷 → 菜单
```

每轮在线匹配包含赛前和赛后两份问卷。共创中的 Stage 是同一关卡的不可变保存版本。直接打开 `/cocreation/` 会创建独立的算法演示（`algorithm_demo`）会话，不进入正式匹配且按 TTL 自动清理。Unity 与后端的交接、正式入场计时、数据边界及完整场景顺序见[现行研究流程](docs/STUDY_PROTOCOL.md)。

## 目录与入口

| 位置 | 内容 |
| --- | --- |
| [Assets/Scripts/](Assets/Scripts/) · [Assets/Scenes/](Assets/Scenes/) | Unity 逻辑与场景，包括[地图生成](Assets/Scripts/Level/Generation/)和[在线匹配控制器](Assets/Scripts/Online/)。启用场景以 [EditorBuildSettings.asset](ProjectSettings/EditorBuildSettings.asset) 为准。 |
| [WebGL 模板](Assets/WebGLTemplates/SokobanPixel/) · [BrowserNavigation.jslib](Assets/Plugins/WebGL/BrowserNavigation.jslib) | Unity WebGL 页面、`TemplateData/sokoban-bridge.js`（协议版本 5）、房间码叠加层与游戏页 Dashboard 密码入口。 |
| [server/core/](server/core/) | 共享纯 Python 3.12 核心库（无 FastAPI/SQL 依赖；Unity [Packages/](Packages/) 仅保留 `manifest.json` 与 `packages-lock.json`）：12×10 网格校验与操作、BFS/推箱求解器、演示/布局生成器、异步结构化 LLM 客户端、时钟与配置脱敏。 |
| [server/](server/) | 统一 FastAPI 后端（`server/main.py` `create_app` 工厂、`settings.py`、`container.py`）：`api/routers/`、`application/{rooms,study,analytics,sessions,dialogue}/`（含 `sessions/{lifecycle,stage,read_model}/`）、`domain/`（含 `{proposals,memory,blueprints}/`）、`i18n/`、`llm/`（原生异步结构化 JSON 管线）、`persistence/`（统一 SQLite WAL 仓储与迁移 CLI）、`evaluations/` 与 `tools/{reliability_report,semantic_eval}.py`。 |
| [web/](web/) | 前端 npm workspace：`packages/ui-core`、`apps/dashboard`（输出 `web/dist/dashboard`，保留 `Sokoban_Tutorial_Bilingual.pdf`）、`apps/cocreation`（输出 `web/dist/cocreation`，工作流拆分于 `src/store/workflows/`）和 `apps/webgl-bridge`。 |
| [deploy/](deploy/) | 统一生产部署与容器配置：`nginx-sokoban.conf`、`sokoban.service`、`Dockerfile`、`compose.yaml`、`deploy.ps1`。 |
| [tests/](tests/) | 统一 pytest 回归与集成测试套件（`tests/core/`、`tests/server/` 与端到端测试）。 |

## 本地开发

- 使用 Unity `6000.3.28f1` 打开仓库根目录（版本来源见 [ProjectVersion.txt](ProjectSettings/ProjectVersion.txt)）。
- Python 后端使用 Python 3.12 与仓库根目录统一虚拟环境 `.venv/`（依赖声明见 [pyproject.toml](pyproject.toml)）。前端使用 Node.js 与 `web/` npm workspace。
- 本地环境变量由根目录 `.env` 统一加载（模板见 [.env.example](.env.example)，原 `Backend/.env` 与 `CoCreationPrototype/Backend/.env` 已合并至根目录 `.env`）：DG/蓝图管线使用 `DEEPSEEK_API_KEY`（默认 `DEEPSEEK_MODEL=deepseek-v4-flash`），共创管线使用 Moonshot/Kimi 的 `COCREATION_LLM_API_KEY`（默认 `COCREATION_LLM_MODEL=kimi-k2.6`），两者互不回退。使用默认 `TOKEN_SECRET` 时服务启动会打印安全警告；删除接口校验 `X-Delete-Password`。

从仓库根目录在 PowerShell 中执行（已有 `.venv` 和 `web/node_modules` 时可跳过依赖安装）：

```powershell
# 1. 构建前端静态资源与 WebGL 桥接脚本
npm --prefix web install
npm --prefix web run build

# 2. 安装 Python 依赖（如尚未创建 .venv）
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"

# 3. 启动统一 8000 服务（工厂模式）
& .\.venv\Scripts\python.exe -m uvicorn server.main:create_app --factory --port 8000
```

如需将历史 JSONL 研究日志或旧版共创 SQLite 数据导入统一数据库，运行独立迁移 CLI：

```powershell
& .\.venv\Scripts\python.exe -m server.persistence.migrations `
    --db server/data/sokoban.sqlite3 `
    --import-study-logs Backend/study_logs `
    --migrate-cocreation-db CoCreationPrototype/Backend/data/cocreation.sqlite3
```

本地启动后的统一入口（全部位于 `http://127.0.0.1:8000`）：

- Matchmaking Dashboard：`http://127.0.0.1:8000/frontend/`（含教程 PDF `/frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf` 与 JSONL 导出 `/export/study-logs.jsonl`）
- 共创工作台：`http://127.0.0.1:8000/cocreation/`（直接访问自动创建带 TTL 的算法演示会话）
- WebGL 游戏页（本地存在 `WebGLBuild/` 时自动挂载）：`http://127.0.0.1:8000/game/`
- 健康检查：`http://127.0.0.1:8000/health`、`http://127.0.0.1:8000/ready`

正式 Unity 入场与试玩要求见[桥接和会话规则](docs/STUDY_PROTOCOL.md#正式入场与草稿重生成)。

## 本地验证

所有常规回归测试使用注入的离线模型替身、虚拟时钟和临时 SQLite 数据库，**零真实付费模型调用**：

```powershell
& .\.venv\Scripts\ruff.exe check server tests
& .\.venv\Scripts\pytest.exe -q
npm --prefix web run typecheck
npm --prefix web test
npm --prefix web run build
```

Unity 场景与正式路线的验收在 Unity Editor（Game view）和对应 WebGL 环境中进行。Editor 已生成工程时，可用 `dotnet build Assembly-CSharp.csproj -v:minimal` 辅助检查 C# 编译。详见[研究链路验收范围](docs/STUDY_PROTOCOL.md#研究链路验收)。

## 文档导航

- [系统架构](docs/ARCHITECTURE.md) 说明统一单服务拓扑、分层契约、数据库表结构、安全模型（Decision 5B）与部署组件。
- [AGENTS.md](AGENTS.md) 记录 AI 开发规范、授权边界、Unity 编辑与验证报告要求。
- [现行研究流程](docs/STUDY_PROTOCOL.md) 记录正式双人路线、模型隔离、Stage 生命周期与研究数据边界。
- [共创回复可靠性](docs/REPLY_RELIABILITY.md) 记录结构化对话、Policy 2 二选一提案、履约校验、局部恢复与统一发布回滚流程。
- [历史提示词归档](docs/archive/multi-agent/) 保留早期角色与提示词参考资料。
- Git 忽略的本地 `AI_Exchange/` 与 `SERVER.md` 分别保留开发日志交接与运维参考。
