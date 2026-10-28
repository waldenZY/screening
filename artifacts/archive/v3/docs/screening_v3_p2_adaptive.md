# P2 Adaptive 技术方案（已实施）

> 所属产品版本：Screening V3
> Pipeline 配置版本：`config_version=7`
> P2 算法标识：`adaptive_challenge_batches_2`
> 文档状态：当前已实施口径，核对于 2026-10-14；默认值以 [pipeline.default.json](../config/pipeline.default.json) 为准

## 1. 目标

P2 正式目标为 1,000 条同时通过 Stage1 和 Challenge Judge 的样本。八类 `primary_challenge` 的软下限依次为 50、50、50、50、50、20、5、1，任一类型最多 350。候选不足或覆盖预算耗尽时允许带缺口交付。

P0/P1 与 P2 独立并发构建，各自完成 Stage2、导出和分支校验后即可按配置发布，不等待 P2。当前 Stage1、Stage2 契约以
[技术方案](screening_pipeline_technical_design_v3.md) 和实际 Prompt 为准；Stage2 只处理最终成员。

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

总量已满但某类未达到自身软下限时会在有限尾部预算内补采。新增稀缺类型会在重新选择时替换超过软下限的头部类型，最终总量不超过 1,000；预算结束时可少于目标或带覆盖缺口交付。

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

抽样按 L2、Cluster 两级平方根容量分配，层内用稳定身份哈希排序；候选规划在相同输入和配置下可复现，LLM 判定仍取决于模型响应。文本去重采用三个集合共用的规范化精确去重及近重复确认：同 L2、双字 Jaccard ≥ 0.45、IDF 加权 2/3-gram Jaccard ≥ 0.80、编辑相似度 ≥ 0.90。去重在 Stage1 前完成；仅召回达标、确认未达标的对保留并审计。

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

1. 每个 primary 先按下表的独立软下限预留；不足时保留实际数量并记录缺口。按 primary 统计，次要 `challenge_types` 不补足配额。
2. 剩余名额按各 primary 可用量平方根分配。
3. 任一 primary 不超过 350 条。
4. 类型内部再次按 L2、Cluster 分层，并使用稳定文本哈希选择。
5. 每批后从全部 Judge PASS 池重算，支持稀缺类型替换头部类型。

| primary | 默认软下限 |
| --- | ---: |
| `implicit_goal` | 50 |
| `multi_constraint` | 50 |
| `multi_turn_context` | 50 |
| `long_tail_expr` | 50 |
| `ambiguity` | 50 |
| `multi_intent` | 20 |
| `boundary` | 5 |
| `cross_app` | 1 |

合计 276；各类下限满足时，剩余 724 个名额按平方根可用量和多样性规则分配。键须完整、下限为非负整数、总和不超过目标、单项不超过统一上限。

## 6. 自适应补采

```text
总产出率先验       0.10
类型产出率先验     0.0125
先验强度           10
最低产出率估计     0.005
安全系数           1.1
单批最大请求       1,000
补采探索保留       20%
补采轮数           3
实际抽样总预算     20,000
目标达成后尾部预算 1,000
尾部单批上限       250
无进展批次上限     2
```

初始轮使用固定 30/30/20/20。每批完成 Stage1 和 Challenge 后，立即更新来源总产出率、来源到 primary 的产出率、总量缺口和八类软下限缺口。产出率以来源池实际抽取数为分母，因此包含文本去重、Stage1 和 Challenge 三段损耗。补采按缺口及后验产出率路由，同时固定保留 20% exploration。

每批输出可解析的 `P2 batch_progress` JSON，包含 `target_fill` 或 `coverage_tail` 模式、八类最终数量与缺口、三项预算状态和 `next_action`；结束时输出 `P2 adaptive_complete` JSON 及明确停止原因。

初始轮为 `wave=0`，之后最多三轮补采，即 `wave=1..3`；轮数不是批数。`max_sampled` 统计实际抽中的唯一候选，包含随后被去重、Stage1 或 Challenge 拒绝者，不是 LLM 请求次数上限。尾部候选也占总预算，两个预算不相加。批前总量已满才进入 `coverage_tail`；刚填满目标的批次仍属于 `target_fill`。

停止条件为目标和软下限均满足、初始轮及最多三轮补采结束、相关候选耗尽、实际抽样达到 20,000、尾部抽样达到 1,000，或连续两个尾部批次没有缩小八类总缺口。任一尾部批次缩小总缺口会把连续计数归零；Challenge PASS 数量增长但最终类型总缺口不降，仍算无进展。预算或早停触发时允许 P2 带数量或类型缺口交付，继续 Stage2、导出和发布。

日志中的动作码：

| `next_action` / `stop_reason` | 含义 |
| --- | --- |
| `continue_target_fill` | 继续补总量，仅用于批次日志 |
| `continue_coverage_tail` | 继续追补类型覆盖，仅用于批次日志 |
| `target_and_soft_mins_met` | 总量及八类软下限均满足 |
| `max_sampled` | 总抽样预算耗尽 |
| `max_tail_sampled` | 尾部预算耗尽 |
| `stalled_batches` | 连续无进展达到上限 |
| `candidate_exhausted` | 当前可抽取候选耗尽 |
| `max_rounds` | 补采轮数结束 |

最终 `P2 adaptive_complete` 重复给出最终数量、类型缺口、预算用量及停止原因，详细批次记录保存在 `adaptive_summary.json`。

## 7. 产物与恢复

- Challenge 有独立 Prompt 哈希及 `quality/challenge_*` 任务、attempts、缓存命中和结果账本；三个质量阶段共用本 run 的 `quality/llm_result_cache.sqlite`，P2 批级检查点位于 `P2/checkpoints/`。各分支保存自身任务引用，新 run 不自动共享旧 run 的 LLM 缓存。
- `adaptive_challenge_batches_2` 同时进入总 run contract 和 P012 构建契约，旧配置和旧检查点不会按新配额语义续跑。
- 中断恢复复用已完成结果，不重复标注调用；启动仍有一次 LLM 预检。总状态为 `failed` 时须显式指定兼容的原 `--run-id`，其他状态的自动选择规则见 [配置手册](screening_pipeline_configuration_v3.md#23-严格续跑条件)。
- 总预算、尾部预算和连续无进展批次数进入检查点，续跑不会重置。
- `screened_schema_version=7`：P2 保存三个 Challenge 字段和 `selection.challenge_task_id`；P0/P1 的 `challenge` 为 `null`。质量契约为 9，报告契约为 10，labeled v5 仍为 14 字段；其他版本见 [文档索引](README.md)。
- P2 允许 `supplement_wave`；采样与 Challenge 算法不因 labeled v5 新增的两个下游标签改变。
- P2 汇总写入 `P2/adaptive_summary.json`，总报告写入 `p2_diagnostics`。
- 去重数值复验兼容 `1e-12` 以内的 0/1 边界误差，不放宽确认阈值、不改写旧审计或检查点；不升级规则或数据契约。

## 8. 报告指标

- 四来源池的容量、计划请求、实际抽取、去重后、Stage1 通过、Judge 通过和最终入集；
- 每轮每批总产出率、来源产出率和来源到 primary 的产出率；
- 每批候选规划、Stage1、Challenge 和最终重选的墙钟耗时及汇总；
- 八类最终数、软下限缺口、单类占比及停止原因；
- 多标签分布和组合；
- Judge 任务量、缓存命中、失败、重试和时延；
- Judge PASS 但未入集的数量；
- 最终数量、`target_gap` 和候选耗尽状态；
- 总抽样与尾部预算的已用、剩余、批次上限、无进展批数和预算停止原因；
- Challenge 标签与 Stage2 issue 的组合诊断。

## 9. 实现位置

- `screening/candidate/p2_adaptive.py`：候选池、分层抽样、后验产出率和最终选择；
- `screening/quality/two_stage.py`：Stage1/Challenge/Stage2 统一缓存与重试；
- `screening/pipeline/orchestrator.py`：批次、补采、checkpoint 和最终重选；
- `screening/delivery/screened.py`：canonical Challenge 契约；
- `screening/delivery/report.py`：P2 诊断；
- `screening/delivery/publish.py`：Challenge 账本和发布前校验；
- `screening/artifacts/prompts/quality_challenge.md`：冻结 Prompt。
