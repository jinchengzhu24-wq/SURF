# Sokoban AI Demo 服务器运维与部署

本文只记录服务器、反向代理、进程托管、发布和回滚信息。产品流程、共创规则和 Unity 场景说明见 [README.md](README.md)。

## 公网入口

正式入口统一使用 HTTPS 根域名：

```text
https://sokobanaidemo.top/game/
https://sokobanaidemo.top/frontend/
https://sokobanaidemo.top/cocreation/
https://sokobanaidemo.top/frontend/tutorial/Sokoban_Tutorial_Bilingual.pdf
```

Cloudflare 对外提供 443 HTTPS，源站通过 HTTP 回源。`www`、旧 IP 和其他非规范 Host 由 Nginx 保留路径并返回 `308` 到根域名；公开链接不使用 8000、8010 或 16384 端口。SSH、SCP 和运维登录仍使用服务器 IP，不使用公网域名。

当前服务器项目目录：

```text
/root/SURF
```

本地项目目录示例：

```text
D:\Sokoban_AI_Demo
```

## 端口与进程

```text
22    SSH
80    源站 Nginx HTTP 监听
16384 源站 Nginx 额外回源/测试监听
443   Cloudflare HTTPS 边缘端口（源站不直接监听）
8000  sokoban-backend，FastAPI 匹配、Dashboard 和 WebGL 上游
8010  sokoban-cocreation，8010 共创服务上游
```

Nginx 将 `/game/`、`/frontend/` 和根 API 转发到 `127.0.0.1:8000`，将 `/cocreation/` 转发到 `127.0.0.1:8010`。用户不应直接访问 8000、8010 或 16384。

## systemd 服务

两个 Python 服务由 systemd 托管，已设置开机启动，并在异常退出后自动重启。不要在 SSH 会话里另外启动常驻的 `python app.py` 或 `uvicorn`，否则会与托管进程争抢端口。

| 服务 | 工作目录 | 端口 | 生产配置 |
| --- | --- | ---: | --- |
| `sokoban-backend` | `/root/SURF/Backend` | 8000 | `/root/SURF/Backend/.env` |
| `sokoban-cocreation` | `/root/SURF/CoCreationPrototype/Backend` | 8010 | `/root/SURF/CoCreationPrototype/Backend/.env` |

常用命令：

```bash
systemctl status sokoban-backend sokoban-cocreation nginx
systemctl is-enabled sokoban-backend sokoban-cocreation
systemctl restart sokoban-backend
systemctl restart sokoban-cocreation
systemctl reload nginx
```

查看最近日志或持续跟踪：

```bash
journalctl -u sokoban-backend --since "1 hour ago"
journalctl -u sokoban-cocreation --since "1 hour ago"
journalctl -u nginx --since "1 hour ago"
journalctl -u sokoban-cocreation -f
```

生产环境变量中的公开地址应保持：

```text
COCREATION_PUBLIC_BASE_URL=https://sokobanaidemo.top/cocreation
COCREATION_WEBGL_BASE_URL=https://sokobanaidemo.top/game/
COCREATION_ALLOWED_ORIGINS=https://sokobanaidemo.top,https://www.sokobanaidemo.top
COCREATION_ONLINE_MATCH_SYNC_URL=http://127.0.0.1:8000
```

不要把 `.env`、API Key、SQLite 数据库或研究日志提交到 Git。

## Nginx 配置

仓库中的配置模板：

```text
CoCreationPrototype/Deployment/nginx-sokoban.conf
```

配置要点：

- 源站监听 80 和 16384，不在源站配置证书或 `listen 443`。
- Cloudflare 的 `X-Forwarded-Proto: https` 被转换为公开协议；非 HTTPS 公网请求返回 `308` 到 `https://sokobanaidemo.top$request_uri`。
- `/cocreation/` 的长请求使用 320 秒代理预算。
- 共创入口 HTML 使用 `no-cache, must-revalidate`；带版本号的 JS/CSS/WebGL 资源可缓存。

修改 Nginx 后先检查再加载：

```bash
nginx -t
systemctl reload nginx
```

## 发布方式

### 8000 后端、Dashboard 或 WebGL

本地 `deploy_scp.ps1` 会上传 8000 后端文件、`Frontend/` 和完整 `WebGLBuild/`；它不会上传 8010，也不会替代 systemd。

```powershell
cd D:\Sokoban_AI_Demo
.\deploy_scp.ps1
```

脚本默认通过 SSH/SCP 连接服务器 IP。上传后按修改范围操作：

```bash
# 修改 Backend 或 requirements.txt 后
/root/SURF/Backend/venv/bin/python -m pip install -r /root/SURF/Backend/requirements.txt
systemctl restart sokoban-backend

# 只修改 Frontend 或 WebGLBuild 时无需重启 Python 服务
```

Unity WebGL 必须由用户使用项目规定的 Unity 版本构建后，上传完整 `WebGLBuild/`；不要只上传单个 loader、wasm 或 data 文件。当前发布缓存键为 `iframe-host-v5-20260918-2`。

### 8010 共创服务

8010-only 发布前先备份 SQLite，然后只上传变更的 `CoCreationPrototype` 文件；不要使用 8000/WebGL 上传脚本，也不要重置数据库。

```bash
cp -a /root/SURF/CoCreationPrototype/Backend/data/cocreation.sqlite3 \
  /root/SURF/CoCreationPrototype/Backend/data/cocreation.sqlite3.backup-$(date -u +%Y%m%dT%H%M%SZ)

/root/SURF/CoCreationPrototype/Backend/venv/bin/python -m pip install \
  -r /root/SURF/CoCreationPrototype/Backend/requirements.txt
systemctl restart sokoban-cocreation
systemctl status sokoban-cocreation --no-pager
```

正式实验进行中不要覆盖数据库或切换整套 WebGL/8010 版本。若需要发布 Nginx 配置，先备份 `/etc/nginx`，通过 `nginx -t` 后再 reload。

## 备份与回滚

发布前至少备份：

```text
/root/SURF/CoCreationPrototype/Backend/data/cocreation.sqlite3
/root/SURF/CoCreationPrototype/Backend/.env
/root/SURF/Backend/.env
/etc/nginx/
/etc/systemd/system/sokoban-backend.service
/etc/systemd/system/sokoban-cocreation.service
/root/SURF/WebGLBuild/
```

回滚时必须成套恢复与版本匹配的 8010 后端、前端/iframe 资源、WebGLBuild、Nginx 配置和环境变量，然后分别重启对应 systemd 服务。不要把旧 IP 版前端、域名版 8010 或不同桥接协议的 WebGLBuild 混用。

## 健康检查与发布验收

```bash
curl -fsS https://sokobanaidemo.top/health
curl -fsS https://sokobanaidemo.top/ready
nginx -t
systemctl is-active sokoban-backend sokoban-cocreation nginx
```

公网检查：

- 根域名的 `/game/`、`/frontend/`、`/cocreation/` 返回 200。
- `www` 和旧 IP 对相同路径返回 308，并保留原路径。
- 入口 HTML 重新验证缓存；版本化静态资源、Unity loader/data/framework/wasm 返回 200。
- 浏览器控制台没有 Mixed Content、CORS、Cookie 或 iframe origin 错误。
- WebGL 与 8010 使用同一发布版本；重生成期间 8000 日志不应新增 LLM 规划请求。

8010 的普通日志也可通过 systemd 查看；8000 的应用轮转日志位于：

```text
/root/SURF/Backend/logs/backend.log
```

## 不再使用的旧方式

服务器端 `deploy_github`、`deploy_scp` 和手动常驻 `uvicorn` 不再是当前发布链路；不要根据旧笔记执行这些命令。当前唯一的进程重启入口是 systemd，当前唯一的本地批量上传脚本是 `deploy_scp.ps1`。
