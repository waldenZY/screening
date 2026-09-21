# P2 Adaptive 技术方案（已实施）

> 所属产品版本：Screening V3
> Pipeline 配置版本：3
> P2 算法标识：`adaptive_challenge_batches_1`
> 文档状态：当前已实施口径，更新于 2026-09-21

## 1. 目标

P2 正式目标为 2,000 条同时通过 Stage1 和 Challenge Judge 的样本。八类 `primary_challenge` 各有软下限 100，任一类型最多 700。候选不足时允许少于 2,000 条，并报告缺口。

P0 不受本次改造影响。Stage1、Stage2 的规则保持不变，Stage2 只处理最终成员。

## 2. 数据流

```text
四类候选池
→ 30/30/20/20 分批抽样与集合内去重
→ Stage1
→ Stage1 PASS 进入 Challenge Judge
→ 按 primary_challenge 建立合格池
→ 软下限、单类上限和总量选择
→ 最多三轮定向/探索补采
→ 最终 P2 成员
→ Stage2
→ screened canonical、legacy、报告
```

总量已满但某类不足 100 时仍会补采。新增稀缺类型会在重新选择时替换超过软下限的头部类型，最终总量仍保持 2,000。

## 3. 候选来源

| 来源 | 定义 | 初始份额 |
| --- | --- | ---: |
| `complex` | `complexity >= moderate` | 30% |
| `multi_turn` | `sample_type=multi_turn` | 30% |
| `boundary` | `cluster_probability < 0.5` | 20% |
| `exploration` | 不属于前三池的剩余候选 | 20% |

候选可同时属于多个来源。`source_tags` 保存全部归属，`sampled_from` 保存实际抽取来源；产出率只归因到 `sampled_from`。30/30/20/20 是初始请求的计划份额，来源容量不足、来源重叠和去重可能让单批实际份额偏离，系统会在后续批次按剩余容量回填。`frequency` 不再作为硬过滤条件，只用于诊断和多样性分析。

报告中的四个 `candidate_capacity` 是各来源池独立容量，重叠样本会同时计入多个池，
因此四项不能相加当作候选并集大小；抽取后的漏斗只按唯一 `sampled_from` 归因。

抽样按 L2、Cluster 两级平方根容量分配，层内用稳定身份哈希排序；相同输入和配置可复现。文本去重继续使用 P2 Jaccard 规则。

## 4. Challenge Judge

输入仅包含：

```text
sample_type
context_turns
target_query
L1
L2
```

不提供 probability、frequency 和候选来源。输出严格为：

```json
{
  "is_challenge": true,
  "primary_challenge": "multi_turn_context",
  "challenge_types": ["multi_turn_context", "ambiguity"]
}
```

`is_challenge=false` 时必须输出 `primary_challenge=null`、`challenge_types=[]`。为 true 时，types 包含 1 至 3 个不重复枚举，primary 必须位于其中并排第一，其余标签按固定枚举顺序排列。不输出 `challenge_level`。

八类枚举：

```text
implicit_goal
multi_constraint
multi_turn_context
long_tail_expr
ambiguity
multi_intent
boundary
cross_app
```

primary 按“移除后最能降低样本难度的类型”选择；无法区分时按以下优先级：

```text
multi_turn_context
multi_intent
cross_app
multi_constraint
ambiguity
implicit_goal
boundary
long_tail_expr
```

类型必须兼容 Stage1：多轮样本在已提供上下文下完整；歧义仍能确定核心目标；多意图均与当前 L2 兼容；边界样本的当前 L2 仍为 exact。

## 5. 配额与最终选择

1. 每个 primary 先预留最多 100 条；不足时保留实际数量并记录缺口。
2. 剩余名额按各 primary 可用量平方根分配。
3. 任一 primary 不超过 700 条。
4. 类型内部再次按 L2、Cluster 分层，并使用稳定文本哈希选择。
5. 每批后从全部 Judge PASS 池重算，支持稀缺类型替换头部类型。

## 6. 自适应补采

```text
总产出率先验       0.10
类型产出率先验     0.0125
先验强度           10
最低产出率估计     0.005
安全系数           1.1
单批最大请求       2,000
补采探索保留       20%
补采轮数           3
```

初始轮使用固定 30/30/20/20。每批完成 Stage1 和 Challenge 后，立即更新来源总产出率、来源到 primary 的产出率、总量缺口和八类软下限缺口。产出率以来源池实际抽取数为分母，因此包含文本去重、Stage1 和 Challenge 三段损耗。补采按缺口及后验产出率路由，同时固定保留 20% exploration。

停止条件为目标和软下限均满足、完成三轮、相关候选耗尽或无未评估候选。不使用连续零命中硬早停。

## 7. 产物与恢复

- Challenge 具有独立 Prompt SHA、任务账本、attempts、缓存命中账本、结果账本、SQLite 缓存和批级 checkpoint。
- `adaptive_challenge_batches_1` 同时进入总 run contract 和 P012 构建契约，算法变化不会误续跑旧检查点。
- 中断恢复复用已完成结果，不重复调用。
- screened canonical schema v3：P2 保存三个 Challenge 字段和 `challenge_task_id`；P0/P1 的 `challenge` 为 `null`。
- P2 允许 `supplement_wave`；legacy 28 字段保持不变。
- P2 汇总写入 `P2/adaptive_summary.json`，总报告写入 `p2_diagnostics`。

## 8. 报告指标

- 四来源池的容量、计划请求、实际抽取、去重后、Stage1 通过、Judge 通过和最终入集；
- 每轮每批总产出率、来源产出率和来源到 primary 的产出率；
- 八类最终数、软下限缺口、单类占比及停止原因；
- 多标签分布和组合；
- Judge 任务量、缓存命中、失败、重试和时延；
- Judge PASS 但未入集的数量；
- 最终数量、`target_gap` 和候选耗尽状态；
- Challenge 标签与 Stage2 issue 的组合诊断。

## 9. 实现位置

- `screening/candidate/p2_adaptive.py`：候选池、分层抽样、后验产出率和最终选择；
- `screening/quality/two_stage.py`：Stage1/Challenge/Stage2 统一缓存与重试；
- `screening/pipeline/orchestrator.py`：批次、补采、checkpoint 和最终重选；
- `screening/delivery/screened.py`：canonical Challenge 契约；
- `screening/delivery/report.py`：P2 诊断；
- `screening/delivery/publish.py`：Challenge 账本和发布前校验；
- `screening/artifacts/prompts/quality_challenge.md`：冻结 Prompt。
