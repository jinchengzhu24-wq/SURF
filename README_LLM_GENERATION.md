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
后端校验蓝图；失败时根据 idea 选择 contextual fallback
  ↓
Unity 应用蓝图和本地强制约束
  ↓
在蓝图指定的 archetype 内为每个候选重新采样模板变体
  ↓
生成外墙、障碍、水域、目标、玩家和箱子
  ↓
本地求解、走廊验证、难度筛选和质量筛选
  ↓
从合格候选中选择质量最高的地图
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

Unity 默认优先读取缓存中的 LLM 方案；缓存不存在时直接请求后端。默认最多请求两份方案。

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
| `minWaterAreas` | 1–2 | 最少水域放置数量 |
| `maxWaterAreas` | 1–2 | 最多水域放置数量 |
| `minWallObstacleBlocks` | 固定为 2 | 最少内部墙障碍数量 |
| `maxWallObstacleBlocks` | 2–3 | 最多内部墙障碍数量 |
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

## 3. 后端校验与 fallback

后端收到 LLM JSON 后会检查：

- 所有必需字段是否存在；
- 数值是否在允许范围内；
- 最大值是否不小于最小值；
- archetype、layout 和 style 是否为支持的枚举；
- `corridorPlacement` 与 `corridorWidth` 是否一致；
- required corridor 是否具有有效 placement。

如果模型调用失败、JSON 解析失败或方案校验失败，后端不再全局轮换固定方案，而是根据 idea 选择 fallback：

```text
水域、河流、水池相关        → split_route + route_divider
迷宫、绕路、通道相关        → bottleneck_corridor
紧凑、困难、死锁相关        → goal_room
没有明显结构关键词          → open_workshop
```

分类优先级为：

```text
water > maze > compact > default
```

fallback 仍会使用合法的固定难度范围。当前响应没有向 Unity 暴露 `usedFallback`，因此 Unity 会把 contextual fallback 当作普通 LLM 方案继续生成。

相关代码：

- `Backend/app.py::validate_plan`
- `Backend/app.py::fallback_plan`
- `Backend/app.py::get_contextual_fallback_plan`

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

这项约束同时存在于提示词、后端 fallback 和 Unity 客户端覆盖逻辑中。

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
由本地求解与质量评分选择最佳候选
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
9. 从箱子位于目标的状态开始执行反向拉动，构造初始箱子位置；
10. 检查箱子、玩家、水域和墙体 tile 规则；
11. 构建最终地图行。

LLM 蓝图模式下，内部墙通常先于水域和目标放置。地图生成完毕后仍必须通过本地求解器验证。

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
水格数量 ≥ 4
surrounded wall 数量 ≥ 1
两箱交互分数 ≥ 4
质量总分 ≥ 220
近期结构相似度 < 86
```

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

生成器会保留合格候选，并从采样结果中选择质量最高的地图，而不是接受第一个可解地图。

## 9. 严格、放宽与重试

每份 LLM 蓝图首先进入 strict 模式，默认最多尝试 300 个候选。

strict 失败后进入 relaxed-blueprint 模式，主要调整：

- 降低最低解法步数；
- 提高最高解法步数；
- 降低最低推动次数；
- 提高最高推动次数；
- 放宽 reverse pulls 范围。

放宽模式仍然保留：

- LLM 指定的 archetype；
- 同 archetype 内的多模板采样；
- required corridor；
- LLM 质量门槛。

如果 required corridor 仍然失败，系统不会取消该硬要求。

如果走廊不是 required，放宽模式失败后可以进入本地 algorithm fallback。整份方案失败后，`LevelLoader` 会请求下一份 LLM 蓝图。默认两份蓝图均失败后停止生成，并且不会加载旧地图。

## 10. 失败日志解读

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
| `ReversePulls` | 未达到反向拉动目标 |
| `TileRules` | 水域或墙体 tile 规则失败 |
| `rejectedBySolve` | 求解器未找到解或达到搜索上限 |
| `rejectedByDifficulty` | 步数或推动次数超出范围 |
| `rejectedByQuality` | 未达到 LLM 质量门槛 |
| `rejectedByCorridor` | required corridor 验证失败 |

## 11. 当前已知限制

- LLM 只能选择高层结构类别，不能指定具体地图坐标；
- `style` 和 `designNote` 不影响生成；
- obstacle 和 water style 是位置偏好，不是严格路线保证；
- 当前提示词固定生成偏难关卡，无法忠实实现简单、无水或低障碍 idea；
- fallback 尚未向 Unity 明确标记来源；
- 第二次 LLM 方案请求尚未携带第一次本地生成的失败统计；
- required corridor 的验证目前不能完整证明玩家或箱子确实从一侧穿越到另一侧；
- 本地求解和候选生成是同步执行的，极端情况下可能造成明显卡帧。

## 12. 修改后的生效方式

- 修改 Unity C# 后，Unity 编辑器会自动重新编译；
- 修改 `Backend/prompt.py` 或 `Backend/app.py` 后，必须重启后端服务；
- 只修改模板或生成规则时，不需要修改 LLM JSON 协议。

