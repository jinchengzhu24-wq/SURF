# Sokoban LLM 关卡生成逻辑

本文档说明当前项目中 LLM 模式的实际生成流程。LLM 不直接生成地图格子，而是生成一份高层设计蓝图；Unity 本地生成器根据蓝图创建候选地图，并负责可解性验证与质量筛选。

## 总体流程

```text
用户地图 idea
  ↓
整理原始想法、选择方向、最新调整和历史反馈
  ↓
请求 LLM 生成高层蓝图 JSON
  ↓
后端校验蓝图；失败返回 502/503，不伪造蓝图
  ↓
Unity 应用蓝图和本地强制约束
  ↓
在蓝图指定的 archetype 内为每个候选重新采样模板变体
  ↓
生成外墙、障碍、水域、目标、玩家和箱子
  ↓
本地求解、走廊验证、难度筛选和质量筛选
  ↓
LLM 模式从质量最高的三张合格候选中等概率随机选择一张
  ↓
若本轮至少一份真实蓝图有效、但所有模板落实均失败，清除蓝图约束并运行一次算法降级
```

### 场景游玩路线

正式创作与研究流程：

```text
Menu → Questionnaire(Before) → Creative_WorkShop → Expansion → Custom_Level → Refinement
```

Refinement 分数达标后的路线：

```text
Refinement → Questionnaire(After) → Menu
```

Refinement 分数未达标时进入调整循环：

```text
Refinement → Adjustment → Custom_Level → Refinement
```

```text
Refinement → Design interpretation → Refinement
Adjustment → Design interpretation → Adjustment
```

## 1. 请求上下文

LLM 请求可以携带以下内容：

- 原始用户 idea；
- 用户选择的设计方向；
- 最新调整；
- 历史调整；
- refinement 反馈；
- idea、session 和场景标识。

提示词中的优先级为：

```text
可解性和生成器支持范围
> 最新用户调整
> 用户选择的设计方向
> 原始用户 idea
> 历史调整和 refinement 反馈
> 通用难度与质量偏好
> 多样性要求
```

LLM 关卡模式不读取方案缓存。每次尝试都会直接请求远程后端，以便确认本轮 LLM 确实可用。默认最多尝试两次真实远程请求。

后端会从用户原始 idea、历史调整和最新调整中识别明确的“无水域”与“无内部墙”要求。判断按最新调整、历史调整倒序、原始 idea 的顺序进行；AI 生成的扩展方向和自动 refinement 文本不会触发零值约束。较新的水域或墙体要求可以覆盖旧要求。

### 创意拓展中的 adaptation

当原始 idea 中的内容无法由当前生成器直接实现，或必须被删除、替换、简化、间接表达时，LLM 必须在候选方向 `description` 的第一句给出 adaptation 说明：

```text
适配：将不支持的原始元素转换为当前生成器能够实现的空间压力。
```

常见触发内容包括传送门、钥匙、门、开关、敌人、计时器、冰面、移动墙、动态地形、不同箱子类型、超过两个箱子或目标、非 12×10 地图、多条或弯曲走廊、精确坐标、精确对齐、固定 T/L/S 形墙以及无法支持的水域形状。适配后的描述只能使用静态墙、水域、目标布局、直线走廊、路线选择、站位压力和推箱顺序等受支持表达，不能继续声称原机制仍会触发、移动、传送或改变地图。

如果 idea 能被现有结构直接表达，则不显示 adaptation。调整难度、紧凑程度、水域数量、目标分布，以及明确要求无水域或无内部墙，均属于当前直接支持的要求，本身不会触发 adaptation。

`adaptation` 当前由提示词要求生成，并非独立响应字段；后端不会单独校验其存在。因此它用于向用户解释语义转换，但不具备程序级强制保证。

相关代码：

- `Assets/Scripts/LLM/LLMLevelDesignClient.cs`
- `Assets/Scripts/Level/LevelLoader.cs`
- `Backend/prompt.py`

## 2. LLM 输出内容

LLM 输出的是 `LevelDesignPlan`，不是地图行、坐标或 tile grid。

当前地图基础规格固定为：

```text
地图尺寸：12×10
箱子数量：2
整体定位：偏难的经典 Sokoban
```

主要数值范围：

| 字段 | 允许范围 | 含义 |
| --- | --- | --- |
| `minSolutionSteps` | 18–30 | 最少玩家移动步数 |
| `maxSolutionSteps` | 32–50 | 最多玩家移动步数 |
| `minPushes` | 8–16 | 最少推箱次数 |
| `maxPushes` | 14–28 | 最多推箱次数 |
| `minWaterAreas` | 明确要求无水时为 0，否则 1–2 | 最少水域放置数量 |
| `maxWaterAreas` | 明确要求无水时为 0，否则 1–2 | 最多水域放置数量 |
| `minWallObstacleBlocks` | 明确要求无内部墙时为 0，否则固定为 2 | 最少内部墙障碍数量 |
| `maxWallObstacleBlocks` | 明确要求无内部墙时为 0，否则 2–3 | 最多内部墙障碍数量 |
| `minReversePulls` | 14–24 | 反向构造的最少拉动次数 |
| `maxReversePulls` | 24–40 | 反向构造的最多拉动次数 |

结构字段：

| 字段 | 可选值 | 实际作用 |
| --- | --- | --- |
| `archetype` | `goal_room`, `bottleneck_corridor`, `split_route`, `open_workshop` | 决定结构模板类别 |
| `targetLayout` | `clustered`, `split_pair`, `edge_cluster` | 优先决定目标点布局 |
| `obstacleStyle` | `central_baffle`, `side_choke`, `goal_guard` | 调整内部墙障碍的位置评分 |
| `waterStyle` | `corner_pool`, `side_pool`, `route_divider` | 调整水域的位置评分 |
| `corridorPlacement` | `none`, `center`, `side` | 是否生成硬走廊及其位置 |
| `corridorWidth` | `0`, `1`, `2` | 走廊宽度；没有走廊时必须为 0 |
| `corridorOrientation` | `horizontal`, `vertical`, `any` | 走廊方向 |
| `corridorRole` | `visual_only`, `player_route`, `required_box_route` | 走廊用途 |
| `corridorPriority` | `preferred`, `required` | 走廊是否为硬要求 |

`style` 和 `designNote` 只用于描述、记录和展示，不直接影响地图生成。

零值是用户硬约束而不是随机范围：明确要求无水时，水域范围固定为 `0–0`；明确要求无内部墙时，墙障碍范围固定为 `0–0`，同时关闭 corridor，只保留闭合外壳。如果没有明确要求，后端仍会拒绝 LLM 无故返回的零值。

## 3. 后端校验与错误返回

后端收到 LLM JSON 后会检查：

- 所有必需字段是否存在；
- 数值是否在允许范围内；
- 最大值是否不小于最小值；
- archetype、layout 和 style 是否为支持的枚举；
- `corridorPlacement` 与 `corridorWidth` 是否一致；
- required corridor 是否具有有效 placement。

关卡计划接口不再生成 contextual fallback：

```text
缺少 DEEPSEEK_API_KEY               → HTTP 503
模型调用、JSON 解析或蓝图校验失败    → HTTP 502
真实 LLM 蓝图通过校验               → HTTP 200 + LevelDesignPlan
```

503 或 502 在 Unity 中都算作真实 LLM 尝试失败。Unity 会继续下一次远程请求；如果本轮从未收到有效蓝图，则停止生成，不运行本地替代地图，也不加载旧地图。

创意方向扩展接口仍保留自己的文案 fallback；它与关卡计划接口无关。

相关代码：

- `Backend/app.py::validate_plan`
- `Backend/app.py::create_level_plan`

## 4. 中央一格宽通道

系统没有要求所有地图默认使用一格宽通道。正常蓝图允许：

```text
corridorPlacement=none, corridorWidth=0
```

或者在存在走廊时选择宽度 1 或 2。

当最新用户调整同时明确表达“中央”和“狭窄通道”时，系统会将其解释为硬要求：

```text
archetype=bottleneck_corridor
obstacleStyle=central_baffle
corridorPlacement=center
corridorWidth=1
corridorPriority=required
```

除非用户明确要求两格宽。常见触发词包括：

```text
narrow corridor
narrow passage
one-tile
single-tile
窄道
狭窄通道
单格通道
瓶颈
```

这项约束存在于提示词和 Unity 客户端覆盖逻辑中。后端不再为关卡计划构造 fallback 蓝图。

## 5. Unity 应用蓝图

Unity 收到方案后会：

1. 应用步数、推动次数、水域、墙障碍和 reverse pulls 范围；
2. 标准化 archetype、target layout、障碍和水域风格；
3. 设置走廊字段；
4. 开启 LLM 质量门槛；
5. 根据最新用户调整再次检查中央窄通道要求。

相关代码：

- `Assets/Scripts/Level/LevelGenerator.cs::ApplyPlan`
- `Assets/Scripts/LLM/LLMLevelDesignClient.cs::ApplyLatestAdjustmentConstraints`

## 6. 模板选择

当前共有 16 个结构模板，每种 archetype 各 4 个。

LLM 只选择 archetype，不直接选择具体模板变体。生成每个候选时，Unity 都会在指定 archetype 对应的 4 个变体中重新采样：

```text
LLM 选择 split_route
  ↓
候选 1 使用 split_route 变体 A
候选 2 使用 split_route 变体 D
候选 3 使用 split_route 变体 B
……
  ↓
由本地求解与质量评分保留高质量候选；LLM 模式从前三名中随机选择
```

因此一轮生成不再绑定单个随机模板。`bottleneck_corridor` 模板名称本身也不会自动生成一格宽硬通道；真正的硬通道由 corridor 字段决定。

相关代码：

- `Assets/Scripts/Level/LevelGenerationTemplates.cs`
- `Assets/Scripts/Level/LevelGenerator.cs::TryGenerateWithCurrentRules`

## 7. 单个候选的构造顺序

每个候选大致按以下顺序构造：

1. 创建 `12×10` 外墙；
2. 如果请求走廊，应用走廊模板；
3. 按蓝图放置内部墙障碍；
4. 放置水域；
5. 优先按照 `targetLayout` 放置两个目标；
6. 如果指定布局无法实现，回退到模板目标锚点；
7. 如果仍然失败，使用普通随机目标放置；
8. 放置玩家；
9. 从箱子位于目标的状态开始，枚举当前所有合法反向拉动并随机执行其中一个，逐步构造初始箱子位置；
10. 检查箱子、玩家、水域和墙体 tile 规则；
11. 构建最终地图行。

LLM 蓝图模式下，内部墙通常先于水域和目标放置。地图生成完毕后仍必须通过本地求解器验证。

由于墙体早于实际目标放置，墙体路线评分会先使用当前结构模板的目标锚点估算目标距离和目标间走廊；实际目标已经存在的模式仍直接使用实际目标位置。

墙体规则仍禁止可玩区域内部的 `2×2` 实心墙和所有 `2×3` 及更大的连续双排墙；仅当一个局部 `2×2` 墙块紧邻地图外部空白时，才将其视为外壳或通道与外壳的连接并允许。

反向构造不会先随机选择箱子和方向再消耗无效尝试。每一步都会复用现有站位、可达性、墙体、水域、目标和其他箱子检查来建立合法操作池，再从池中等概率随机选择；操作池为空或仍未达到目标拉动次数时，该候选继续按 `ReversePulls` 失败处理。该改动不放宽任何地图规则、难度范围或质量门槛。

## 8. 求解与筛选

候选地图构造成功后依次检查：

1. 是否与近期地图重复或过于相似；
2. `LevelSolver` 是否可以求解；
3. required corridor 是否满足验证条件；
4. 实际解法步数是否在蓝图范围内；
5. 实际推箱次数是否在蓝图范围内；
6. 是否满足障碍影响和 LLM 质量门槛。

当前 LLM 质量门槛包括：

```text
strict：水格数量 ≥ 4，surrounded wall ≥ 1，两箱交互分数 ≥ 4，质量总分 ≥ 220，结构相似度 < 86
relaxed：水格数量 ≥ 2，surrounded wall ≥ 0，两箱交互分数 ≥ 2，质量总分 ≥ 170，结构相似度 < 94
```

明确的零水域蓝图不检查最低水格数量；明确的零内部墙且无 corridor 的蓝图不检查最低 surrounded wall 数量。质量总分、两箱交互、结构相似度和可解性门槛保持不变。

质量分综合考虑：

- 解法步数；
- 推箱次数；
- reverse pulls；
- 求解器搜索状态数；
- 水格与水域数量；
- surrounded walls；
- 障碍影响；
- 目标距离；
- 近期结构相似度。

生成器会保留合格候选，而不是接受第一个可解地图。LLM strict 和 relaxed 模式会继续尝试直到收集到三张合格候选并满足现有采样窗口，随后按现有质量排序保留前三名并等概率随机选择一张；达到最大尝试次数后仍不足三张时，从实际存在的 Top 1-2 中选择，不会放宽任何质量条件。非 LLM 算法模式和最终算法 fallback 仍然选择质量最高的地图。

## 9. 严格、放宽与重试

每份 LLM 蓝图首先进入 strict 模式，默认最多尝试 300 个候选。

strict 失败后进入 relaxed-blueprint 模式，主要调整：

- 最低解法步数最多降低 10，最低保留 12；最高解法步数提高 20；
- 最低推动次数最多降低 6，最低保留 4；最高推动次数提高 10；
- reverse pulls 最低值最多降低 10，最低保留 8；最高值不再提高，避免放宽阶段反而更难构造；
- 使用较低但仍存在的 LLM 质量门槛。

放宽模式仍然保留：

- LLM 指定的 archetype；
- 同 archetype 内的多模板采样；
- required corridor；
- LLM 质量门槛。

单份蓝图的 relaxed 模式失败后不会立即进入 algorithm fallback。`LevelLoader` 会继续请求下一份真实蓝图；required corridor 失败也按“当前模板无法落实该蓝图”处理。

所有远程尝试结束后：

- 如果本轮至少收到过一份通过后端校验的真实 LLM 蓝图，但所有有效蓝图都无法由模板落实，则清除 corridor、蓝图结构、LLM 质量门槛和普通蓝图数值，恢复算法基线，并运行一次现有算法 fallback；用户明确要求的零水域、零内部墙约束会继续保留；
- 如果两次请求都失败或响应非法，则禁止算法 fallback，停止生成且不加载旧地图；
- 第一份蓝图有效但落实失败、第二次远程请求失败时，因为本轮已经证明 LLM 成功过，仍允许最终算法 fallback。

最终算法 fallback 可以放弃 required corridor，但不能恢复用户明确禁止的水域或内部墙。它生成的是明确标记的降级地图，不声称实现了其余 LLM 蓝图。

## 10. 生成来源

Console 与 `LevelStudyRecorder` 使用以下来源字符串：

| 来源 | 触发条件 | 是否保存 applied-plan context |
| --- | --- | --- |
| `LLMGuided` | 有效 LLM 蓝图被 strict 或 relaxed 成功落实 | 是 |
| `LLMSuccessAlgorithmFallback` | 本轮至少一份 LLM 蓝图有效，但所有蓝图落实失败，最终算法降级成功 | 否 |
| `Algorithm` | 非 LLM 模式直接使用算法生成 | 否 |

LLM 模式全部远程请求失败时不会生成地图，因此不会把失败结果记成 LLM 或 fallback 来源。

## 11. 失败日志解读

典型日志：

```text
candidateFailures=BaseGrid=300
```

表示 300 个候选全部在基础地图阶段失败，尚未进入求解。

常见统计字段：

| 字段 | 含义 |
| --- | --- |
| `BaseGrid` | 外墙、尺寸、走廊或基础墙规则失败 |
| `WallObstacles` | 内部墙障碍无法放置 |
| `WaterAreas` | 水域无法放置或破坏连通性 |
| `Targets` | 目标点无法放置 |
| `Player` | 玩家起点无法放置 |
| `ReversePulls` | 合法反向拉动池耗尽，或未达到反向拉动目标 |
| `TileRules` | 水域或墙体 tile 规则失败 |
| `rejectedBySolve` | 求解器未找到解或达到搜索上限 |
| `rejectedByDifficulty` | 步数或推动次数超出范围 |
| `rejectedByQuality` | 未达到 LLM 质量门槛 |
| `rejectedByCorridor` | required corridor 验证失败 |

## 12. 当前已知限制

- LLM 只能选择高层结构类别，不能指定具体地图坐标；
- `style` 和 `designNote` 不影响生成；
- obstacle 和 water style 是位置偏好，不是严格路线保证；
- 当前提示词仍固定生成偏难关卡；除明确的零水域、零内部墙要求外，尚不能忠实实现其他简单或低障碍档位；
- 第二次 LLM 方案请求尚未携带第一次本地生成的失败统计；
- required corridor 的验证目前不能完整证明玩家或箱子确实从一侧穿越到另一侧；
- 本地求解和候选生成是同步执行的，极端情况下可能造成明显卡帧。

## 13. 修改后的生效方式

- 修改 Unity C# 后，Unity 编辑器会自动重新编译；
- 修改 `Backend/prompt.py` 或 `Backend/app.py` 后，必须上传到部署服务器并重启后端服务；
- 只修改模板或生成规则时，不需要修改 LLM JSON 协议。
