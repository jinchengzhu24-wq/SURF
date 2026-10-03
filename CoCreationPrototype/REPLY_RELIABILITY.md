# 8010 回复可靠性与流程恢复

2026-10-03。公开接口、数据库表结构、模型隔离、研究记录及 Unity 协议保持兼容。

## 验收边界

| 检查 | 处理 |
| --- | --- |
| 当前地图事实、原文证据、明确方向/限制、授权、冻结差异、求解 | 不可靠的结论或操作不得交付；错误前提引出的相邻因果句一起清理 |
| 必需分歧/意图反馈状态、精确用户证据、人工编辑观察与比较 | 使用原流程次数和同一截止时间修复，失败保持原状态；不得删除必需字段来通过 |
| 句数、反思深度、重复、非必需卡片和表达长度 | 单独记录，局部修复或省略可选卡片；保留独立成立的可靠正文 |
| 可选 DesignContext 补丁、坐标链接、问题回答复核 | 无效字段丢弃或保持原状态，不影响已完成主流程 |

最低正文要求仍包含实质回复、可理解的分析和可靠事实。复核明确指出正文没有回答问题时仍须重试。单个 JSON 对象可去除代码围栏；多个对象、截断、缺失语义不猜测补全。

要求复核给出受影响 ID、问题类别、用户原文和修复指令。修复锁定其他合格要求，完整集合重新核对证据与覆盖。仅完整通过复核的记录可进入 `requirements_verified` 内部审计缓存；键覆盖 StageSnapshot、用户来源/内容、澄清回答、正式决定、DesignContext 投影、语言和规则/提示版本。

“否”是决定证据，不是原始设计要求。源卡、原始用户请求、主题回答和 canonical 理由共同构成后续方案的证据。历史错误的助手来源只有通过同请求、同 Stage 的真实用户记录才能恢复。旧候选指纹在模型、修复、搜索及末端验收共同排除。

可靠理解后没有合格候选时，预留的时间可生成一次经过地图、语言和伪成功检查的 Kimi 失败分析。无紫卡、无地图修改；choice_pending 仍等待可靠解决。要求解释不可靠、权限不成立或上游故障不得走此恢复分支。

## 预算和提交

- 普通聊天和当前要求策略的新方案：总预算 120 秒，HTTP 入口建立内部 116 秒绝对截止时间，所有内部调用共享。
- 普通正文最多三次；要求编译/复核和理由评审保留各自两次上限。旧冻结卡的执行路径保持兼容。
- 可选工作累计最多 10 秒，剩余不足 20 秒不启动；必需语义复核使用主预算。
- 正文与必需状态按逻辑单位提交；失败可以保留已保存的用户轮次，重试复用原键，不重复写卡片、问题或版本。
- 手工保存的可解 Stage 保留，观察与比较两条评审消息完整提交；失败重试评审，不重复创建 Stage。
- 翻译每个轮次独立提交；部分成功的错误包含已完成和待重试 ID，只读刷新保留成功结果，失败项显示原文。
- 浏览器成功响应必须满足实际端点契约，125 秒消息超时覆盖发送、读取正文和解析；异常后只读刷新确认已提交状态。重试闭包捕获原文本、动作、Stage、草稿和幂等键。

## 流程覆盖与回归

| 流程 | 当前检查/恢复与回归入口 |
| --- | --- |
| 普通聊天、地图、路线 | 正文与意图组件独立验收；清理错误前提及相邻因果结论；`test_reply_reliability`、`test_llm_client` |
| 方案按钮、建议、三问、短回答 | 持久主题和实际可见问题计数；同键重试不重复计数；`test_llm_client`、`test_sessions` |
| 要求、合同、候选、搜索 | 两轮局部要求修复、缓存失效、共享语义约束、候选不可行分析；`test_design_requirements`、`test_revision_workflow`、`test_reply_reliability` |
| 紫卡执行、接受/拒绝 | 最新来源、冻结重放、最终求解与原子提交；`test_sessions`、`test_revision_workflow` |
| 首次质疑、理由、继续讨论、“是/否”、替代 | 理由地图验证进入两次评审；保留 review_pending；原请求/理由恢复与提前排除旧候选；`test_sessions`、`test_reply_reliability` |
| 人工编辑和其后分歧 | 保存与评审分离、两条消息完整恢复；acknowledged 的最新原话证据仍必需；`test_sessions`、`test_llm_client` |
| Stage 开场、恢复版本、历史 | 只在明确允许的开场使用快照兜底，Stage 1 固定指导一次；历史版本隔离；`test_sessions`、`test_llm_client` |
| 橙卡和意图反馈 | 修正文不检查卡片，修卡片不检查正文；必需冲突卡不可省略；确认复核失败不升级记忆；`test_reply_reliability`、`test_design_context`、`test_sessions` |
| DesignContext、问题进度 | 可选故障保持问题状态，确定性忽略/恢复，历史不吸收后代证据；`test_design_context`、`test_sessions` |
| 翻译 | 合格字段锁定，按来源快照重验，提示不发送快照内部信息；逐条保留成功项；`test_sessions`、`test_llm_client` |
| 保存、恢复、试玩、最终提交 | 幂等、并发、旧 Stage、deadline、确定性操作保护；`test_sessions`、`test_app` |
| Demo/正式、8000/Unity 边界 | deadline 和研究同步差异不变，模型/DG 隔离，host 协议保持版本 5；8010 全量及 `Backend/test_*.py` |
| 前端错误恢复 | HTML/非法 JSON、空或缺字段 2xx、响应正文超时、原键重试、诊断字段；`Frontend/tests/test_controls.cjs` |

测试仅使用固定模型输出、临时数据库与故障注入，不需要真实 API 或模型网络调用：

```powershell
python -m unittest discover -s CoCreationPrototype/Backend/tests -p "test_*.py"
node --test CoCreationPrototype/Frontend/tests/*.cjs
python -m unittest discover -s Backend -p "test_*.py"
```

新增样例直接比较旧的整轮阻断条件与组件恢复结果：可靠正文加坏可选卡片、已验证要求加一个错误要求、坏地图理由后第二次修复、翻译部分成功、人工编辑评审失败后恢复完整消息。授权、必需分歧证据、不可解/反向修改及旧卡仍由现有反例保护。测试成功说明故障路径覆盖，不能证明生产百分比。

## 日志与指标

组件诊断包括组件、结果、失败代码、依赖与可修复性；生成日志记录各阶段尝试/耗时、保留/修复/省略，以及正文和方案结果。审计新增事件使用现有表，无迁移：

- `requirements_verified`：仅完整复核通过的要求与缓存键，不显示为卡片。
- `reply_delivery_outcome`：生成、正文交付、完整质量、可执行方案、组件结果与兜底来源。
- `message_generation_failed` / `reply_generation_failed`：失败尝试；既有 `challenge_reason_review_pending` 也进入报告。

只读汇总命令（时间为 UTC；应从本次部署时刻起）：

```powershell
python CoCreationPrototype/Backend/reliability_report.py path/to/cocreation.sqlite3 --since 2026-10-03T09:00:00
```

按会话和幂等键合并重试，分别统计首请求与重试后模型生成、可靠正文和完整质量通过率。完整质量表示记录的组件没有剩余质量/省略问题，是运行检查指标，不是人工审美评分。快照开场兜底与确定性回执单独统计，不计作 Kimi 正文成功。已请求方案单独统计验证成功数及比例；翻译的独立记录不冒充新的聊天正文。少量真实请求不能支持稳定成功率结论。

## 8010 部署与回滚

1. 完成上述回归与差异检查，确认生产代码基线没有另行变更。
2. SQLite 在线 backup API 生成一致备份；保存所有将替换的代码、静态文件及 SHA256 清单。备份目录不得进入 Git。
3. Windows SSH/SCP 显式使用 `%USERPROFILE%\.ssh\sokoban` 和 `IdentitiesOnly=yes`。仅上传相关 `CoCreationPrototype` 文件及新增模块，不上传 env、数据库、日志或 Unity 输出。
4. 校验上传文件，替换后重启 `sokoban-cocreation`。只改 8010，不重启 8000、不调用整仓 deploy_scp、不构建 WebGL。
5. 检查源站和公网 health/ready、HTML/脚本缓存版本、未授权错误 JSON、历史会话只读构建。不得创建会删除旧演示数据的线上测试 Demo。
6. 若启动或只读验证失败，恢复代码与静态文件并重启；数据库备份用于灾难恢复，不自动覆盖期间产生的研究记录。
