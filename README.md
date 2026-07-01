# Sokoban AI Demo 部署说明

当前线上前端地址：

```text
http://111.231.136.4:8000/frontend/
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

现在主要分三种情况：

1. 只改前端：用本地 Windows CMD 的 `scp` 上传前端文件，然后刷新浏览器。
2. 改了后端，并且服务器能连 GitHub：本地 push 后，在服务器运行 `deploy_github`。
3. 改了后端，但 GitHub 不稳定或不想用 GitHub：本地用 `scp` 上传文件，然后在服务器运行 `deploy_scp`。

## 情况一：只改前端

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

只改前端时通常不需要重启后端，也不需要运行服务器脚本。

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
Frontend/index.html
Frontend/app.js
Frontend/styles.css
```

上传完成后，在服务器 OrcaTerm 里执行：

```bash
cd /root/SURF
./deploy_scp
```

如果不想用 PowerShell 脚本，也可以手动执行下面的 `scp` 命令。

先在 Windows CMD 上传后端文件：

```cmd
scp D:\Sokoban_AI_Demo\Backend\app.py root@111.231.136.4:/root/SURF/Backend/app.py
```

如果同时改了前端，也一起上传：

```cmd
scp D:\Sokoban_AI_Demo\Frontend\index.html root@111.231.136.4:/root/SURF/Frontend/index.html
scp D:\Sokoban_AI_Demo\Frontend\app.js root@111.231.136.4:/root/SURF/Frontend/app.js
scp D:\Sokoban_AI_Demo\Frontend\styles.css root@111.231.136.4:/root/SURF/Frontend/styles.css
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
- 改了后端启动方式
- 后端接口没有响应
- 想让服务器重新加载后端代码

不需要重启后端：

- 只改了 `Frontend/index.html`
- 只改了 `Frontend/app.js`
- 只改了 `Frontend/styles.css`

前端文件由后端静态服务读取，上传覆盖后刷新浏览器即可。

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
