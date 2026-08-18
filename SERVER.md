# Sokoban AI Demo 部署说明

当前线上地址：

```text
http://111.231.136.4:8000/frontend/
http://111.231.136.4:8000/game/
http://111.231.136.4:8000/frontend/Images/Routing.png
http://111.231.136.4/cocreation/
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
Frontend/styles.css
Frontend/Images/
```

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
http://111.231.136.4:8000/game/
```

如仍显示旧版本，使用 `Ctrl + F5` 强制刷新。

## 情况一：只改数据前端

适用文件：

```text
Frontend/index.html
Frontend/app.js
Frontend/styles.css
```

在 Windows CMD 里执行：

```cmd
scp D:\Sokoban_AI_Demo\Frontend\index.html root@111.231.136.4:/root/SURF/Frontend/index.html
scp D:\Sokoban_AI_Demo\Frontend\app.js root@111.231.136.4:/root/SURF/Frontend/app.js
scp D:\Sokoban_AI_Demo\Frontend\styles.css root@111.231.136.4:/root/SURF/Frontend/styles.css
```

上传完成后，直接刷新：

```text
http://111.231.136.4:8000/frontend/
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
http://111.231.136.4:8000/health
http://111.231.136.4:8000/ready
```

`/health` 表示进程可访问；`/ready` 还会检查 API Key、模型配置和日志目录。运维日志位于 `Backend/logs/backend.log`，单文件 5 MB，保留 5 份轮转文件。当前后端只能使用一个 Uvicorn worker。

完整错误码和排查方法见 `README_LLM_ERRORS.md`。
