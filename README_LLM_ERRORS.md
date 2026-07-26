# LLM 报错与排查说明

本文用于排查 Unity 调用后端及后端调用 DeepSeek 时出现的错误。

## 推荐排查顺序

1. 在 Unity Console 中找到 `requestId`、`responseCode`、`code`、`stage`、`attemptsUsed` 和 `retryable`。
2. 检查后端进程：

   ```text
   http://111.231.136.4:8000/health
   ```

3. 检查 LLM 配置及日志目录：

   ```text
   http://111.231.136.4:8000/ready
   ```

4. 使用 Unity 中的 `requestId` 查询服务器日志：

   ```bash
   cd /root/SURF
   grep '对应的requestId' Backend/logs/backend.log
   ```

5. 如果最近修改过后端，重新上传依赖文件、安装锁定依赖并重启：

   ```powershell
   .\deploy_scp.ps1
   ```

## 标准错误格式

后端 LLM 错误使用以下结构：

```json
{
  "detail": {
    "code": "MODEL_VALIDATION_FAILED",
    "stage": "blueprint_validation",
    "message": "minPushes=18 is outside 8-16",
    "requestId": "4dbe...",
    "retryable": true,
    "attemptsUsed": 2
  }
}
```

- `requestId`：用于关联 Unity Console 和服务器日志。
- `attemptsUsed`：本次操作已经消耗的模型调用次数。
- `retryable`：系统是否允许继续使用剩余模型调用预算。
- `stage`：失败发生在连接、JSON 解析、蓝图校验等哪个阶段。

## 后端 LLM 错误码

| 错误码 | HTTP | 后端行为 | 含义 | 处理方法 |
|---|---:|---|---|---|
| `CONFIGURATION_ERROR` | 503 | 否 | 服务器没有有效的 `DEEPSEEK_API_KEY` | 检查 `Backend/.env`，重启后端，再检查 `/ready` |
| `UPSTREAM_AUTHENTICATION_FAILED` | 503 | 否 | API Key 无效、过期或没有模型权限 | 更换有效 Key，确认账号和模型权限；不要把 Key 发到聊天或日志中 |
| `UPSTREAM_RATE_LIMIT` | 503 | 可补试 1 次 | DeepSeek 返回 429，配额或并发受限 | 等待后重试，检查余额、速率限制和并发请求量 |
| `UPSTREAM_CONNECTION_ERROR` | 502 | 可补试 1 次 | 后端无法连接 DeepSeek | 检查服务器网络、DNS、防火墙和 DeepSeek 服务状态 |
| `UPSTREAM_TIMEOUT` | 504 | 可补试 1 次 | 单次模型请求超过后端超时 | 检查服务器到 DeepSeek 的延迟；通过 `requestId` 查看两次尝试的耗时 |
| `UPSTREAM_SERVER_ERROR` | 502 | 可补试 1 次 | DeepSeek 返回 5xx | 通常是上游临时故障，稍后重试并保留 `requestId` |
| `UPSTREAM_REQUEST_REJECTED` | 502 | 否 | DeepSeek 返回不可重试的 4xx | 检查模型名、请求参数及当前 API 兼容性 |
| `MODEL_JSON_INVALID` | 502 | 可补试 1 次 | 模型返回内容不是合法 JSON | 后端会自动发送一次修复请求；持续出现时查看服务器中的模型输出片段 |
| `MODEL_VALIDATION_FAILED` | 502 | 可补试 1 次 | JSON 合法，但字段、范围或修改合同不符合规则 | 根据 `stage` 和 `message` 定位字段；常见原因是数值越界或 HA 合同非法 |
| `INTERNAL_ERROR` | 500 | 否 | 后端出现未分类异常 | 用 `requestId` 查看服务器堆栈，确认部署文件和依赖版本一致 |

## 常见 HTTP 和 Unity 现象

### `responseCode=0` 或 `Request timeout`

表示 Unity 没有收到 HTTP 响应，常见原因：

- 后端进程未运行；
- IP、端口或接口地址错误；
- 防火墙或服务器网络中断；
- Unity 等待超过客户端超时；
- 场景切换或对象销毁导致请求被中止。

先在运行 Unity 的同一台电脑访问 `/health`。如果服务器日志中找不到该 `requestId`，请求通常没有到达后端。

### `HTTP 502 Bad Gateway`

先查看 Unity Console 中是否包含标准 `code`：

- 有 `code`：后端已收到请求，按上表处理。
- 显示 `UNSTRUCTURED_ERROR`：响应可能来自旧后端、反向代理或非 JSON 错误页，应检查部署版本和服务器日志。

### `HTTP 404` 或 `HTTP 405`

通常表示 Unity 已更新为 POST 接口，但服务器仍运行旧代码，或配置了错误的接口路径。重新上传以下文件并重启：

```text
Backend/app.py
Backend/llm_runtime.py
Backend/prompt.py
Backend/requirements.txt
```

### `HTTP 422 Unprocessable Entity`

请求 JSON 缺少必填字段或字段类型错误。它发生在进入 LLM 前，因此不会消耗模型调用次数。重点检查 Unity 请求体和后端 Pydantic 请求模型是否一致。

### `/health` 为 200，但 `/ready` 为 503

后端进程正在运行，但尚不能处理 LLM 请求。查看 `/ready` 中：

- `apiKeyConfigured`；
- `modelConfigured`；
- `logDirectoryWritable`。

修复对应配置后必须重启后端。

### `Real LLM plan request failed or returned an invalid blueprint`

后端没有返回可用蓝图。Unity 会根据 `attemptsUsed` 计算剩余预算，整个关卡生成过程最多消耗两次模型调用。非重试错误会立即停止。

项目研究规则要求：从未获得合法 LLM 蓝图时，不允许使用算法蓝图兜底。

### `Valid LLM blueprint could not be realized by the current templates`

LLM 蓝图已经通过后端校验，但当前 Unity 生成模板无法落实。如果第一次只消耗了一次模型调用，Unity 会使用剩余一次预算请求新蓝图；预算耗尽后停止。

这通常需要同时检查蓝图约束和 `LevelGenerator` 的模板能力。

### Human 路线显示 `Clarity check failed`

这是请求或服务器错误，玩家输入会保留。查看 Unity Console 中的标准错误详情后重试。

`Human adjustment needs clarification` 不是系统错误，而是用户输入没有达到清晰度评分要求。

### HA Plan 显示请求失败

生成或编辑方案失败时，当前编辑内容会保留。使用 Retry 再试，并通过 Unity Console 的 `requestId` 查询后端错误。

### Expansion 使用备用选项

Expansion 在远程模型失败时允许使用本地备用方向，因此玩家可能没有看到错误页面。后端日志会记录模型错误；只有整个 HTTP 请求失败时，Unity Console 才会记录请求错误。结果中的 `usedFallback=true` 表示没有使用模型生成的方向。

## 部署与启动错误

### `ModuleNotFoundError: llm_runtime`

服务器缺少新共享模块。重新运行：

```powershell
.\deploy_scp.ps1
```

### Python 包版本或导入错误

在服务器项目目录安装锁定依赖：

```bash
cd /root/SURF
python3 -m pip install -r Backend/requirements.txt
./deploy_scp
```

### 日志目录不可写

`/ready` 会返回 `logDirectoryWritable=false`。确认运行后端的 Linux 用户可以创建并写入：

```text
/root/SURF/Backend/logs
```

### 端口 8000 已被占用

通常表示旧后端进程没有正确停止。使用服务器已有的 `deploy_scp` 脚本重启，不要同时启动多个 Uvicorn 实例。

当前 JSONL 文件锁和内存蓝图历史仅支持单个 Uvicorn worker。

## 日志说明

运维日志同时输出到 stdout 和：

```text
Backend/logs/backend.log
```

单个文件最大 5 MB，保留 5 份轮转文件。非法模型输出只在服务器日志中保留最多 1000 个字符，不返回 Unity。日志不会主动记录完整 prompt、API Key 或完整请求 URL。
