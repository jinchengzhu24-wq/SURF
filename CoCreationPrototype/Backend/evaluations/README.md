# 8010 入口语义评测

本文件说明独立入口语义评测的覆盖、离线验收和采样方法。工作台运行契约见 [8010 回复可靠性](../../REPLY_RELIABILITY.md)。

## 当前覆盖

`turn_understanding_v1.json` 包含 36 个明确标为 `synthetic` 的合成案例和两张可解地图：中英文评价、意图、标签解释、思路询问、明确/模糊修改、Proposal 按钮、三问主题中的短回答、保护实体、改变方向及 Stage 切换。

运行入口复用生产 `classify_turn_understanding`，只检查表达行为、所指元素和修改属性的理解。Stage 切换案例检查当前快照构造及入口分类，**不证明完整回复一定纠正了历史坐标**。不满意案例是独立评价，不模拟活跃质疑流程。完整正文、橙卡、分歧、方案授权/执行仍由既有回归和人工复核覆盖。

预期值允许合理的多种表达行为，不逐字匹配回复。`expected`、案例名称、研究评测说明和 `reviewNotes` 不进入模型请求。模型只接收生产函数需要的用户语言、当前 StageSnapshot、Proposal 标志和主题上下文。这里的案例不是从真实参与者记录中导出；以后新增真实案例须先脱敏并标明来源，保持真实记录原样。

## 离线验收与比较

从项目根目录执行：

```powershell
python CoCreationPrototype/Backend/semantic_eval.py validate
python -m unittest discover -s CoCreationPrototype/Backend/tests -p "test_semantic_eval.py"
python -m unittest discover -s CoCreationPrototype/Backend/tests -p "test_reliability_report.py"
```

`validate` 验证案例结构与地图可解性，调用模型次数为零。测试使用固定输出与临时文件，不读取生产数据库或调用真实模型。

保存的模型输出格式为：

```json
{
  "suiteHash": "validate 返回的完整 suiteHash",
  "repeats": 3,
  "model": "kimi-k2.6",
  "sourceHash": "按路径排序拼接 cocreation/llm/ 顶层 *.py 的字节后计算的 SHA-256",
  "runs": [
    {
      "caseId": "zh_crowded",
      "repeat": 1,
      "result": {
        "acts": ["evaluation"],
        "elements": ["unknown"],
        "evidenceSpan": "这里看起来太挤了。",
        "directionSufficient": false,
        "mapRelated": true,
        "changes": []
      }
    }
  ]
}
```

这只是格式示例，不是实测结果。运行错误用 `errorCode` 记录，缺失样本留在总分母，不能只挑成功输出计算。拒绝未知案例、重复样本和套件不一致的数据；同案例重复测量报告不稳定表现。比较需使用同一套件及相同重复次数，报告逐案例退化/改善和模型、代码来源信息。

```powershell
python CoCreationPrototype/Backend/semantic_eval.py score CoCreationPrototype/Backend/evaluation_runs/baseline.json
python CoCreationPrototype/Backend/semantic_eval.py compare CoCreationPrototype/Backend/evaluation_runs/baseline.json CoCreationPrototype/Backend/evaluation_runs/candidate.json
```

## 可选真实 Kimi 采样

有本轮明确授权时才运行 `run --live`。它使用进程环境中的 8010 Kimi 配置调用提供商，不读取 8000 的 DeepSeek 配置。该工具不访问工作台 HTTP、会话或 SQLite，不改变地图，也不把研究评测预期发给模型。

```powershell
New-Item -ItemType Directory -Force CoCreationPrototype/Backend/evaluation_runs
python CoCreationPrototype/Backend/semantic_eval.py run --live --repeats 3 --output CoCreationPrototype/Backend/evaluation_runs/baseline.json
```

36 案例重复三次意味着 108 次逻辑分类请求，生产函数内部还可能重试，产生真实费用与耗时。输出路径必须不存在；每完成一个样本保存一次，中断后已有结果可离线评分。运行结果目录已加入 Git 忽略。采样分数用于比较同一套件的入口理解，线上交付与语义质量另行复核。旧采样需先核对套件哈希与重复次数。
