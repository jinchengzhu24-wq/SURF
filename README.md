# SURF · Sokoban 人机共创与在线挑战

SURF 是一个 Unity 2D 推箱子研究项目。参与者通过设计问题和 AI 首版规划得到一张地图，在网页工作台和 AI 一起修改，再用定稿地图进行双人挑战。

## 系统组成

| 组件 | 职责 |
| --- | --- |
| Unity / WebGL | 问卷和匹配导航、DG 交互、首版生成与验证、Stage 试玩、在线挑战。 |
| 8000 | DG 首版理解、蓝图规划、在线匹配与结果、研究 Dashboard。 |
| 8010 | 共创工作台、Stage 版本、人工编辑与 AI 提案、共创研究记录。 |

8000 的 Draft 首版理解助手从 DG 回答生成难度和布局建议。关卡蓝图规划助手把确认后的理解转换为蓝图，Unity 据此生成并验证地图。8010 的共创聊天助手理解当前 Stage 的设计请求，共创关卡修改助手按授权合同给出操作候选。生成器、地图校验和求解由确定性程序执行。

## 当前在线流程

```text
Unity 赛前问卷 → 大厅与匹配 → DG 与首版生成
  → 8010 预览 Draft → 进入共创并创建 Stage 1
  → 聊天、编辑、提案与试玩 → 确认最终 Stage → 提交设计意图
  → Unity 等待对手 → 挑战 → 结果 → 赛后问卷 → 菜单
```

每轮在线匹配包含赛前和赛后问卷。8010 的 Stage 是同一关卡的保存版本。独立演示入口提供算法 Draft，不进入正式匹配。Unity、8000 和 8010 的交接、正式入场计时、数据归属及完整场景顺序见[现行研究流程](docs/STUDY_PROTOCOL.md)。

## 目录与入口

| 位置 | 内容 |
| --- | --- |
| [Assets/Scripts/](Assets/Scripts/) · [Assets/Scenes/](Assets/Scenes/) | Unity 逻辑与场景，包括[地图生成](Assets/Scripts/Level/Generation/)和[匹配控制器](Assets/Scripts/Online/)。启用场景以 [EditorBuildSettings.asset](ProjectSettings/EditorBuildSettings.asset) 为准。 |
| [WebGL 模板](Assets/WebGLTemplates/SokobanPixel/) · [BrowserNavigation.jslib](Assets/Plugins/WebGL/BrowserNavigation.jslib) | iframe 桥接、浏览器房间码和游戏页的 Dashboard 入口。 |
| [Backend/app.py](Backend/app.py) · [sokoban_backend/](Backend/sokoban_backend/) · [Frontend/](Frontend/) | 8000 启动入口、API 与研究投影、Dashboard。 |
| [CoCreationPrototype/Backend/app.py](CoCreationPrototype/Backend/app.py) · [cocreation/](CoCreationPrototype/Backend/cocreation/) · [CoCreationPrototype/Frontend/](CoCreationPrototype/Frontend/) | 8010 启动入口、后端业务和共创工作台。后端包中的 `api/`、`domain/`、`persistence/`、`llm/` 分别负责接口、业务校验、存储及模型调用。 |
| [只读可靠性报告](CoCreationPrototype/Backend/reliability_report.py) · [语义评测入口](CoCreationPrototype/Backend/semantic_eval.py) | 独立运行的审计统计和入口理解评测。 |

## 本地开发

- 用 Unity `6000.3.28f1` 打开仓库根目录，版本来源是 [ProjectVersion.txt](ProjectSettings/ProjectVersion.txt)。
- 后端开发使用 Python 3.12 和两个独立虚拟环境。网页使用原生 ES Modules，验证需要支持 `node --test` 的 Node.js。
- 两个后端分别读取各自目录的本地 `.env`。8000 默认使用 `deepseek-v4-flash`（`DEEPSEEK_MODEL`），实际模型请求需要 `DEEPSEEK_API_KEY`。8010 默认使用 Moonshot 的 `kimi-k2.6`（`COCREATION_LLM_MODEL`），配置示例见 [.env.example](CoCreationPrototype/Backend/.env.example)。本地开发使用独立的数据路径与开发配置。

从仓库根目录在两个 PowerShell 终端分别执行。已有可用虚拟环境可跳过创建与安装，保留本地配置和启动命令。

```powershell
python -m venv Backend/.venv
& .\Backend\.venv\Scripts\python.exe -m pip install -r Backend/requirements.txt
& .\Backend\.venv\Scripts\python.exe Backend/app.py
```

```powershell
python -m venv CoCreationPrototype/Backend/.venv
& .\CoCreationPrototype\Backend\.venv\Scripts\python.exe -m pip install -r CoCreationPrototype/Backend/requirements.txt
$env:COCREATION_PUBLIC_BASE_URL = 'http://127.0.0.1:8010'
$env:COCREATION_DATABASE_PATH = './data/cocreation-local.sqlite3'
& .\CoCreationPrototype\Backend\.venv\Scripts\python.exe CoCreationPrototype/Backend/app.py
```

8000 的 Dashboard 位于 `http://127.0.0.1:8000/frontend/`，有现成 `WebGLBuild/` 时也挂载 `/game/`。8010 本地工作台位于 `http://127.0.0.1:8010/`，根入口会在所配置的 8010 库创建演示会话。生成算法 Draft 可离线进行，实际模型聊天和开场需要配置 8010 的模型。正式 Unity 入场与试玩需要符合[桥接和会话规则](docs/STUDY_PROTOCOL.md#正式入场与草稿重生成)。

## 本地验证

```powershell
& .\Backend\.venv\Scripts\python.exe -B -m unittest discover -s Backend -p 'test_*.py'
& .\CoCreationPrototype\Backend\.venv\Scripts\python.exe -B -m unittest discover -s CoCreationPrototype/Backend/tests -p 'test_*.py'
node --test 'Frontend/tests/*.mjs' 'CoCreationPrototype/Frontend/tests/*.mjs' 'Tests/WebGLBridge/*.cjs'
Get-ChildItem Frontend/js, CoCreationPrototype/Frontend/js -Recurse -Filter *.js | ForEach-Object { node --check $_.FullName }
& .\CoCreationPrototype\Backend\.venv\Scripts\python.exe -B CoCreationPrototype/Backend/semantic_eval.py validate
```

后端测试注入模型与同步替身，使用临时存储。Unity 场景与正式路线的验收在 Editor 和相应 WebGL 环境进行。Editor 已生成工程时，可用 `dotnet build Assembly-CSharp.csproj -v:minimal` 辅助检查编译。[研究链路验收范围](docs/STUDY_PROTOCOL.md#研究链路验收)

## 文档导航

- [AGENTS.md](AGENTS.md) 记录 AI 开发、授权、Unity 编辑与报告要求。
- [现行研究流程](docs/STUDY_PROTOCOL.md) 记录正式路线、服务边界、Stage 与数据保留。
- [8010 回复可靠性](CoCreationPrototype/REPLY_RELIABILITY.md) 记录对话、A/B 提案、验收、恢复和发布流程。
- [入口语义评测](CoCreationPrototype/Backend/evaluations/README.md) 说明离线校验与比较。Git 忽略的本地 `AI_Exchange/` 保留开发日志和交接。
