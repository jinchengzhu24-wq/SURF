# Sokoban AI Demo 部署说明

当前线上地址：

```text
http://111.231.136.4/frontend/
http://111.231.136.4/game/
http://111.231.136.4/frontend/Images/Routing.png
http://111.231.136.4/frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf
http://111.231.136.4/cocreation/

https://v.wjx.cn/vm/YXvrnKg.aspx#
https://v.wjx.cn/vm/O6tj8nu.aspx#
```

端口说明：`8000` 是 Nginx 转发的内部匹配、dashboard 和 WebGL 上游端口，`8010` 是 Nginx 转发的内部共创服务端口；用户访问时统一使用 Nginx 的 80 端口，因此公开地址不带 `:8000` 或 `:8010`。公开入口为 `http://111.231.136.4/game/`、`http://111.231.136.4/frontend/` 和 `http://111.231.136.4/cocreation/`。

从 WebGL 页面底部的 `DATA DASHBOARD` 按钮进入 Dashboard 时，会先在游戏页面内显示访问密码框；密码通过 8000 的 `/verify-dashboard-password` 校验成功后，才打开 `/frontend/`。取消或校验失败都不会跳转。Dashboard 内的删除/清空操作仍会再次要求原删除密码。直接访问 `/frontend/` 不经过 WebGL 入口时保持原行为。

当前在线路线为 `Menu → Online1 → Online_Lobby → Match_Briefing → DG → DG_Level → CoCreation_Entry → 8010 → Challenge_Waiting → Online_Level → Match_Result → Online2`。Online1 的隐藏第 5 题使用 `q5=<studySessionId>`，Online2 的隐藏第 22 题使用 `q22=<studySessionId>`；问卷通过玩家 `studySessionId` 与 Dashboard 中的玩家记录人工核对，`matchId` 用于识别比赛。Unity Draft 场景已退役，但 8000 Dashboard 仍保留不可变的 `Draft` 研究记录节点。

DG 目前使用四道中立地图问题：首步检查、推箱依赖、空间分布和路线结构。Q1/Q2 只推断 Difficulty，Q3/Q4 只推断 Layout；AI 会返回 reflection、理由和建议，完整链路写入 8000 Draft，不传入 8010。双 `no_preference` 时对应建议保持 `Random`；AI 仅可在明确冲突时将确定性基准上下调整一档。

8000 共创流程仅从 `Menu` 在线入口启动、且玩家在 8010 成功产生 `first_stage` 后才正式记录。DG 中确认 Draft 但未进入 8010 的合格玩家只保留房间生命周期内的临时设置，不写入流程 JSONL；收到 `first_stage` 时才按 Draft → First Stage 顺序落盘。未从 Menu 启动的玩家不写匹配生命周期或共创流程 JSONL；其 8010 同步成功但被直接丢弃，不会中断工作台。Dashboard 默认只显示至少一位玩家同时具有 Menu 起点标记与 `first_stage` 的比赛；缺少新标记的旧记录不会显示。达到门槛但未完整结束的比赛仍显示实际的 `In progress`、`Expired` 或 `Cancelled` 状态，只有两位玩家都提交结果才标记为 `Completed`。Final Stage 对手游玩中的 `R` 重开次数通过 `restartCount` 记录，并在 Challenge maps、Compare player challenges 和 Final map 的 `Opponent restarts` 中显示；旧结果缺少该字段时显示 `-`。

8010 地图修改的失败分为两类：模型/传输错误（超时、连接失败、空响应、非法 JSON 等）第一次返回 `retryable=true`，前端用同一消息幂等键提供一次 Retry；再次失败后保存手动编辑/继续讨论的说明，不再提供 Retry。若已得到有效 `RevisionPlan`，但确定性搜索找不到同时满足要求且可解的候选，则返回 200 的助手说明和 `warning` 提示，不创建提案、不放宽要求、不改变当前 Stage，也不显示 Retry。说明会邀请玩家亲自在编辑器调整，或继续与 AI 商讨如何缩小或重新表述目标。零候选仍会先执行一次内部结构修正；旧会话已保存的 `relaxationOffer` 仍可按旧逻辑读取和确认，但新失败请求不再创建放宽流程。

Online Lobby 的生成房间码位于静态浏览器只读输入框中，可选中后手动复制；加入房间输入框支持粘贴并规范化为六位字母数字码。两个输入框通过 `BrowserNavigation.jslib` 与 Unity 同步，不使用 `COPY CODE` 按钮。若修改此模板或其桥接代码，必须重新构建并上传完整 `WebGLBuild/`。

8000 Dashboard 的 Match ID 和两位玩家 Study Session ID 默认显示前 8 位，复制按钮复制完整值，搜索支持完整值及短值；每个流程节点的 Record details 都显示两位玩家的 Study Session ID。Final map 显示共创耗时，Result submitted 和挑战地图详情继续显示对手游玩时长。Dashboard 不再显示已淘汰的 `AI assistant` 模式，`Designer intention` 的用户可见标签统一为 `Message`；兼容字段仍保留在后端原始记录中。

## 端口速查

```text
80    = HTTP 默认端口（Nginx 公网入口）
443   = HTTPS 默认端口
8000  = FastAPI 常用端口之一（匹配、dashboard 和 WebGL 上游服务）
8010  = 共创服务使用的内部端口
22    = SSH 常用端口
```

服务器项目目录：

```text
/root/SURF
```

本地项目目录示例：

```text
D:\Sokoban_AI_Demo
```

## 更新方式总览

现在主要分四种情况：

1. 只改数据前端：用本地 Windows CMD 的 `scp` 上传 `Frontend` 文件，然后刷新浏览器。
2. 改了后端，并且服务器能连 GitHub：本地 push 后，在服务器运行 `deploy_github`。
3. 改了后端，但 GitHub 不稳定或不想用 GitHub：本地用 `scp` 上传文件，然后在服务器运行 `deploy_scp`。
4. 只改网页游戏：Unity 重新构建后上传整个 `WebGLBuild`，不需要上传 Unity 工程源码。

## 网页游戏需要同步哪些文件

本次首次部署网页游戏时，必须同步：

| 本地文件或目录 | 服务器位置 | 作用 |
| --- | --- | --- |
| `Backend/app.py` | `/root/SURF/Backend/app.py` | 提供 `/game/` 静态路由和 WebGL MIME 类型 |
| `WebGLBuild/` | `/root/SURF/WebGLBuild/` | Unity 生成的完整网页游戏 |

如果同时修改了数据 Dashboard，还需要按实际改动同步：

```text
Frontend/index.html
Frontend/app.js
Frontend/matchmaking.js
Frontend/styles.css
Frontend/Images/
```

如果修改了 WebGL 页面底部的 `DATA DASHBOARD` 链接、访问密码框、房间码原生输入或其桥接，还需要用 Unity 重新构建并同步完整 `WebGLBuild/`；入口来自 `Assets/WebGLTemplates/SokobanPixel/index.html`。

以下文件只参与本地 Unity 开发或构建，不需要上传服务器：

```text
Assets/WebGLTemplates/
Assets/Scripts/
Assets/Scenes/
ProjectSettings/
Packages/
Library/
Temp/
deploy_scp.ps1
```

其中，`deploy_scp.ps1` 是在本地 Windows 上执行的上传脚本；服务器上的 `deploy_scp` 是另一个用于重启后端的 Linux 脚本。

推荐先在 Unity 中重新构建：

```text
D:\Sokoban_AI_Demo\WebGLBuild
```

然后在本地 PowerShell 执行：

```powershell
cd D:\Sokoban_AI_Demo
.\deploy_scp.ps1
```

该脚本会同步核心后端文件、数据前端和完整的 `WebGLBuild`。首次加入 `/game/` 路由后，还需要在服务器执行：

```bash
cd /root/SURF
./deploy_scp
```

以后如果只更新了 Unity 游戏或 WebGL 页面模板，只需重新构建并上传 `WebGLBuild`，通常不需要重启后端。上传后访问：

```text
http://111.231.136.4/game/
```

如仍显示旧版本，使用 `Ctrl + F5` 强制刷新。

## 情况一：只改数据前端

适用文件：

```text
Frontend/index.html
Frontend/app.js
Frontend/matchmaking.js
Frontend/styles.css
```

在 Windows CMD 里执行：

```cmd
scp D:\Sokoban_AI_Demo\Frontend\index.html root@111.231.136.4:/root/SURF/Frontend/index.html
scp D:\Sokoban_AI_Demo\Frontend\app.js root@111.231.136.4:/root/SURF/Frontend/app.js
scp D:\Sokoban_AI_Demo\Frontend\matchmaking.js root@111.231.136.4:/root/SURF/Frontend/matchmaking.js
scp D:\Sokoban_AI_Demo\Frontend\styles.css root@111.231.136.4:/root/SURF/Frontend/styles.css
```

上传完成后，直接刷新：

```text
http://111.231.136.4/frontend/
```

如果浏览器仍显示旧样式，按：

```text
Ctrl + F5
```

只改数据前端时通常不需要重启后端，也不需要运行服务器脚本。

## 情况二：后端改动走 GitHub

适用情况：

- 改了 `Backend/app.py`
- 本地已经 `commit` 并 `push`
- 服务器能正常连接 GitHub

在服务器 OrcaTerm 里执行：

```bash
cd /root/SURF
./deploy_github
```

这个脚本会做这些事：

- 从 GitHub 拉取最新代码
- 检查 `Backend` 和 `Frontend` 必要文件
- 停止旧的后端进程
- 重新启动 uvicorn 后端
- 检查 `/health`

如果卡在 `git fetch` 或 `git pull`，说明服务器连接 GitHub 不稳定，改用 `deploy_scp` 方案。

## 情况三：后端改动走 scp

适用情况：

- 改了 `Backend/app.py`
- 改了 `Backend/llm_runtime.py` 或 `Backend/requirements.txt`
- GitHub 连接失败或不想依赖 GitHub
- 想直接把本地文件覆盖到服务器

推荐在本地 PowerShell 里运行项目自带脚本上传前后端文件：

```powershell
cd D:\Sokoban_AI_Demo
.\deploy_scp.ps1
```

说明：

- `.ps1` 是 PowerShell 脚本的标准后缀名，不是自定义后缀。
- `.\deploy_scp.ps1` 里的 `.\` 表示“当前目录下的这个文件”，这是 Windows PowerShell 的常用写法。
- 服务器 Linux/bash 里通常写 `./deploy_scp`，因为 Linux 路径分隔符是 `/`。

如果 PowerShell 提示不允许执行脚本，改用：

```powershell
cd D:\Sokoban_AI_Demo
powershell -ExecutionPolicy Bypass -File .\deploy_scp.ps1
```

这个本地脚本会上传：

```text
Backend/app.py
Backend/llm_runtime.py
Backend/prompt.py
Backend/requirements.txt
Frontend/index.html
Frontend/app.js
Frontend/matchmaking.js
Frontend/styles.css
Frontend/Images
WebGLBuild
```

上传完成后，在服务器 OrcaTerm 里执行：

```bash
cd /root/SURF
python3 -m pip install -r Backend/requirements.txt
./deploy_scp
```

如果不想用 PowerShell 脚本，也可以手动执行下面的 `scp` 命令。

先在 Windows CMD 上传后端文件：

```cmd
scp D:\Sokoban_AI_Demo\Backend\app.py root@111.231.136.4:/root/SURF/Backend/app.py
scp D:\Sokoban_AI_Demo\Backend\llm_runtime.py root@111.231.136.4:/root/SURF/Backend/llm_runtime.py
scp D:\Sokoban_AI_Demo\Backend\prompt.py root@111.231.136.4:/root/SURF/Backend/prompt.py
scp D:\Sokoban_AI_Demo\Backend\requirements.txt root@111.231.136.4:/root/SURF/Backend/requirements.txt
```

如果同时改了前端，也一起上传：

```cmd
scp D:\Sokoban_AI_Demo\Frontend\index.html root@111.231.136.4:/root/SURF/Frontend/index.html
scp D:\Sokoban_AI_Demo\Frontend\app.js root@111.231.136.4:/root/SURF/Frontend/app.js
scp D:\Sokoban_AI_Demo\Frontend\matchmaking.js root@111.231.136.4:/root/SURF/Frontend/matchmaking.js
scp D:\Sokoban_AI_Demo\Frontend\styles.css root@111.231.136.4:/root/SURF/Frontend/styles.css
```

如果同时更新了网页游戏，先在 Unity 中重新构建，然后上传完整目录：

```cmd
scp -r D:\Sokoban_AI_Demo\WebGLBuild root@111.231.136.4:/root/SURF/
```

然后在服务器 OrcaTerm 里执行：

```bash
cd /root/SURF
./deploy_scp
```

注意：`deploy_scp` 不负责上传文件。它只负责检查服务器上已经存在的文件，并重启后端。

## 什么时候需要重启后端

需要运行 `deploy_github` 或 `deploy_scp`：

- 改了 `Backend/app.py`
- 改了 `Backend/llm_runtime.py`、`Backend/prompt.py` 或 `Backend/requirements.txt`
- 改了后端启动方式
- 后端接口没有响应
- 想让服务器重新加载后端代码

不需要重启后端：

- 只改了 `Frontend/index.html`
- 只改了 `Frontend/app.js`
- 只改了 `Frontend/matchmaking.js`
- 只改了 `Frontend/styles.css`
- 只重新构建并上传了 `WebGLBuild`

数据前端和 WebGL 构建文件都由后端静态服务直接读取，上传覆盖后刷新浏览器即可。

## 脚本权限

如果运行脚本时出现：

```text
Permission denied
```

在服务器里执行：

```bash
cd /root/SURF
chmod +x deploy_github deploy_scp
```

也可以直接用 bash 运行：

```bash
bash deploy_github
bash deploy_scp
```

## 后端状态与运维日志

重启后依次检查：

```text
http://111.231.136.4/health
http://111.231.136.4/ready
```

`/health` 表示进程可访问；`/ready` 还会检查 API Key、模型配置和日志目录。运维日志位于 `Backend/logs/backend.log`，单文件 5 MB，保留 5 份轮转文件。当前后端只能使用一个 Uvicorn worker。

完整错误码和排查方法见 [Old_md/LLM_ERRORS.md](Old_md/LLM_ERRORS.md)。

## Tutorial PDF 静态资源

`Frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf` 由现有 `/frontend/` 静态路由公开为 `http://111.231.136.4/frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf`。浏览器会直接使用内置 PDF 查看器在线打开；只更新教程文件时，上传该目录即可，无需重启 8000 服务。Menu 按钮事件的改动需随下一次 WebGL 构建发布。

## DG Draft research record

The 8000 `POST /online/rooms/{match_id}/draft` record now keeps the four DG answer codes,
the AI reflection and both recommendation rationales, the AI recommendation and source, and
the user's final difficulty and layout. Older clients remain compatible; incomplete records
are marked `draftMetadataComplete: false`. See the current question list in
`Draft_question.md` and the bilingual prompt specification in `Draft_prompt.md`.

DG 的 Difficulty 仅由 Q1/Q2 计算，Layout 仅由 Q3/Q4 计算。每组先按低/中/高方向得到确定性基准：相邻冲突取对应端点，跨两级冲突取中间档；单题 `no_preference` 使用另一题，两题均为 `no_preference` 才是 `Random`。只有两题明确冲突时，AI 可以在该基准上下调整一档；同方向或无偏好时不得调整。Draft 记录实际 AI 推荐值，基准值可由四个答案重新计算。

The 8010 co-creation `final` flow event also carries `coCreationDurationSeconds`, calculated
from the existing ten-minute browser deadline as `600 - remainingSeconds` at final Stage
confirmation and capped at 600 seconds after timeout. The 8000 dashboard shows this value as
`Co-creation time` in the `Final map` details. Opponent-level play time remains the existing
Match Result `result_submitted.durationSeconds` record and is shown in the Result submitted
details and the corresponding challenge map details; it is not duplicated on Final map.
