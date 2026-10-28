# Screening 最终交付数据字段说明（labeled v4）

> 文档状态：labeled v4 历史口径；当前交付见 [labeled v5](../../../docs/screening_labeled_v5_field_dictionary.md)
> 适用版本：Screening v3 / `labeled_v4`
> 更新日期：2026-09-21

## 1. 数据是什么

Screening 最终向下游交付 P0、P1、P2 三份 UTF-8 JSONL 文件。JSONL 表示“一行一条
JSON 对象”，文件最外层没有数组。

| 数据集 | `set` 字段值 | 文件名 | 主要用途 |
| --- | --- | --- | --- |
| P0 冒烟集 | `P0_smoke` | `P0_smoke_set_v4_labeled.jsonl` | 小规模、结构化覆盖，用于快速冒烟验证 |
| P1 基准集 | `P1_benchmark` | `P1_benchmark_set_v4_labeled.jsonl` | 主要基准集，强调 L2 覆盖与分布 |
| P2 挑战集 | `P2_adversarial` | `P2_adversarial_set_v4_labeled.jsonl` | 通过独立 Challenge Judge 的困难样本，用于挑战场景验证 |

每条记录固定 28 个字段。P0/P1 的第 15 个字段叫 `relevance_p0`，P2 的第 15 个字段
叫 `relevance_p2`；其余字段名称和顺序完全一致。

最终交付记录已经全部通过第一阶段准入条件：

```text
category_fit=exact
+ semantic_completeness=self_contained
+ intent_clarity=clear
+ validity=valid
```

P2 还必须满足 `is_challenge=true`。Challenge 的主类型和多标签保存在内部
`screened.canonical.jsonl` 与报告中；为了保持下游 labeled v4 兼容，28 字段文件不新增
Challenge 字段。需要分析 P2 类型构成时，应读取同一 run 的
`screening_run_report.json`，不能从 28 字段反推 Challenge 类型。

## 2. 28 个字段

| 序号 | 字段 | JSON 类型 | 含义与取值 |
| ---: | --- | --- | --- |
| 1 | `text` | string | **实际测试输入**。单轮时等于 `target_query`；多轮时为前序上下文与目标轮按 ` | ` 拼接后的完整会话。下游执行或评测时应优先使用本字段。 |
| 2 | `raw_text` | string | cluster 源数据中的原始 `text`，主要用于追溯；可能保留原始空白、输入标记等，不建议直接当作规范化测试输入。 |
| 3 | `context_text` | string | 目标轮之前的所有有效轮次，按 ` | ` 拼接。单轮样本固定为空字符串；这是唯一允许为空字符串的字段。 |
| 4 | `target_query` | string | 最后一轮用户输入，即本轮需要识别和响应的请求。多轮评测时不能脱离 `context_text` 单独使用。 |
| 5 | `turn_count` | integer | 有效会话轮次数，等于前序轮次数加 1，最小为 1。 |
| 6 | `sample_type` | string enum | 会话类型：`single_turn`（单轮）或 `multi_turn`（多轮）。 |
| 7 | `L1` | string | 一级意图类别名称，来自本次运行使用的 taxonomy。 |
| 8 | `L2` | string | 二级意图类别名称，来自本次运行使用的 taxonomy。 |
| 9 | `cluster_id` | integer | 源样本所属的聚类 ID。labeled v4 最终交付要求为整数。 |
| 10 | `cluster_prob` | number | 样本属于该 cluster 的聚类置信度，范围 `[0,1]`。它不是 L1/L2 标签准确率，也不是质量分。源字段缺失时取 `0.0`。 |
| 11 | `prob_band` | string enum | `cluster_prob` 分档：`high`（`>=0.9`）、`mid`（`0.5~0.9`，不含 0.9）、`boundary`（`<0.5`）。 |
| 12 | `frequency` | integer | 源数据提供的非负频次信号；不是当前交付文件中的重复次数，也不应直接等同于全局线上流量。源字段缺失时取 `1`。 |
| 13 | `freq_band` | string enum | 样本在**同一 cluster 内**的频次分档：`hot`、`warm`、`cold`，具体规则见第 4 节。 |
| 14 | `norm_freq` | number | `frequency` 在同一 cluster 内经 `log1p` 后做 min-max 归一化的结果，范围 `[0,1]`，交付时保留 4 位小数。 |
| 15 | `relevance_p0` / `relevance_p2` | number | 采样相关性分数，范围 `[0,1]`，交付时保留 4 位小数。P0/P1 输出 `relevance_p0`，P2 输出 `relevance_p2`；计算公式见第 4 节。 |
| 16 | `length_bin` | string enum | 规范化、清理后的完整测试文本长度分档：`short`、`mid`、`long`。 |
| 17 | `complexity` | string enum | 基于文本长度、轮数、动作词、连接词等固定规则计算的表面复杂度：`simple`、`moderate`、`complex`。它不是 Agent 任务执行难度。 |
| 18 | `formality` | string enum | 基于语气词、口语词、重复标点、表情等固定规则计算的表达风格：`formal`、`mixed`、`colloquial`。 |
| 19 | `session_id` | string | 源记录的会话标识，用于追溯。它不保证跨文件或跨批次全局唯一，不应单独作为样本主键。 |
| 20 | `set` | string enum | 数据集归属：`P0_smoke`、`P1_benchmark` 或 `P2_adversarial`。 |
| 21 | `quality_category_fit` | string enum | L1/L2 与主要用户目标的匹配程度：`exact`、`partial`、`mismatch`。最终交付记录应为 `exact`。 |
| 22 | `quality_semantic_completeness` | string enum | 完整会话是否足以理解和测试目标：`self_contained`、`context_dependent`、`fragments`。最终交付记录应为 `self_contained`。 |
| 23 | `quality_intent_clarity` | string enum | 用户希望助手完成的行为是否明确：`clear`、`vague`、`no_intent`。最终交付记录应为 `clear`。 |
| 24 | `quality_validity` | string enum | 样本能否形成可验证的助手行为：`valid`、`borderline`、`invalid`。最终交付记录应为 `valid`。 |
| 25 | `quality_classification_difficulty` | string enum | 根据完整会话正确路由到 L1/L2 的难度：`trivial`、`easy`、`moderate`、`hard`、`not_applicable`。 |
| 26 | `quality_task_difficulty` | string enum | 具备完整工具能力的 Agent 执行或回答该请求的难度：`atomic`、`simple`、`moderate`、`complex`、`extreme`、`not_applicable`。 |
| 27 | `quality_issue_types` | array[string] | 第二阶段独立复核发现的问题类型。无问题时固定为 `["none"]`；有问题时可包含一个或多个其他枚举值，但 `none` 不能与其他值共存。 |
| 28 | `quality_overall_score` | integer | 作为测评数据的综合质量分，范围 `1~5`，5 分最高。难度高本身不会导致降分。 |

## 3. 质量字段枚举说明

### 3.1 类别匹配 `quality_category_fit`

| 值 | 含义 |
| --- | --- |
| `exact` | 主要目标完整、明确地属于给定 L2，且 L1/L2 粒度合适 |
| `partial` | 标签与目标相关或覆盖其中一部分，但不是完整、最准确的归属 |
| `mismatch` | 主要目标属于其他类别，或文本没有足够信息支持当前类别 |

### 3.2 语义完整性 `quality_semantic_completeness`

| 值 | 含义 |
| --- | --- |
| `self_contained` | 当前提供的完整会话已经足够理解目标；可以依赖文件中已提供的上下文 |
| `context_dependent` | 存在用户目标，但仍依赖未提供的前文、附件、对象或关键条件 |
| `fragments` | 文本为残句、实体堆叠、严重截断或噪声，无法形成完整表达 |

### 3.3 意图清晰度 `quality_intent_clarity`

| 值 | 含义 |
| --- | --- |
| `clear` | 预期行为明确，允许存在不影响核心目标的次要省略 |
| `vague` | 能看出可能存在目标，但有多个实质解释，或关键动作/对象不明确 |
| `no_intent` | 没有可识别的请求、问题或可测试目标 |

### 3.4 样本有效性 `quality_validity`

| 值 | 含义 |
| --- | --- |
| `valid` | 用户目标可识别，信息足以形成可验证的助手行为 |
| `borderline` | 有一定测试价值，但目标、上下文或文本质量存在实质不确定性 |
| `invalid` | 没有可测试目标，或文本损坏到无法可靠判断 |

### 3.5 分类难度 `quality_classification_difficulty`

| 值 | 含义 |
| --- | --- |
| `trivial` | 高频、模板化或关键词直接，几乎不需要上下文推理 |
| `easy` | 表达有变化或较口语，但类别仍然明确 |
| `moderate` | 需要理解隐含表达、上下文指代或相邻 L2 的细微区别 |
| `hard` | 需要跨多轮纠正、复杂组合语义或较强推理才能确定唯一类别 |
| `not_applicable` | 没有可识别意图时使用；对最终交付样本通常不应出现 |

### 3.6 任务难度 `quality_task_difficulty`

| 值 | 含义 |
| --- | --- |
| `atomic` | 一个明确动作，不需要额外推理或步骤依赖 |
| `simple` | 同一功能域内少量连续操作，或简单内容生成 |
| `moderate` | 需要检索加推理、条件处理，或多个相关步骤 |
| `complex` | 多能力组合，并存在明显的跨步骤依赖 |
| `extreme` | 多领域、重型生成、长期规划或复杂编排 |
| `not_applicable` | 没有可执行或可回答任务时使用；对最终交付样本通常不应出现 |

### 3.7 问题类型 `quality_issue_types`

| 值 | 含义 |
| --- | --- |
| `none` | 未发现质量问题；只能单独出现 |
| `category_mismatch` | 第二阶段独立复核认为主要目标不属于给定类别 |
| `ambiguous_intent` | 意图存在实质歧义 |
| `context_dependent` | 仍依赖输入中未提供的上下文 |
| `truncated` | 文本存在影响理解的截断或不完整 |
| `nonsense` | 文本没有可识别语义或意图 |
| `emotional_only` | 只有情绪表达，没有可测试目标 |
| `too_brief` | 文本过短且没有实际语义内容；短而完整的指令不属于此项 |
| `multi_intent` | 包含两个及以上可独立执行的用户目标 |

### 3.8 综合质量分 `quality_overall_score`

| 分数 | 含义 |
| ---: | --- |
| 5 | 优秀：类别准确、语义完整、意图清晰、有效且没有质量问题 |
| 4 | 良好：存在一个轻微、明确的问题，但仍可稳定用于测评 |
| 3 | 合格：存在较明显问题，仍有使用价值但需要注意 |
| 2 | 较差：存在严重问题，不适合常规测评使用 |
| 1 | 不合格：没有可靠的可测试目标 |

`quality_overall_score=5` 时，`quality_issue_types` 必须为 `["none"]`。第二阶段是独立
复核，合法的第二阶段结论即使与第一阶段存在分歧，也不会把样本从最终集合中删除。

## 4. 固定特征计算口径

这些字段由确定性规则计算，不是 LLM 主观评分。以下为当前默认规则；规则或权重改变
会形成新的运行版本。

### 4.1 频次分档与归一化

对每条样本，在其所属 cluster 内计算：

```text
percentile = cluster 内 frequency 严格小于当前值的记录数 / cluster 总记录数
```

- `percentile >= 0.8`：`freq_band=hot`
- `0.2 <= percentile < 0.8`：`freq_band=warm`
- `percentile < 0.2`：`freq_band=cold`

`norm_freq` 先对 `frequency` 取 `log1p`，再在同一 cluster 内做 min-max 归一化；同一
cluster 内所有 frequency 都相同时取 `1.0`。因此 `frequency`、`freq_band`、
`norm_freq` 只能在相应 cluster 的统计口径下解释，不适合直接跨 cluster 比较热度。

### 4.2 Relevance

当前默认公式为：

```text
relevance_p0 = 0.6 × cluster_prob + 0.4 × norm_freq

relevance_p2 = 0.5 × (1 - cluster_prob) + 0.5 × (1 - norm_freq)
```

`relevance_p0` 越高，表示越偏向高聚类置信度、高频样本；`relevance_p2` 越高，表示
越偏向低聚类置信度、低频样本。P1 沿用 `relevance_p0` 字段名是 labeled v4 的兼容
约定，不表示该字段只适用于 P0。

### 4.3 文本特征

- `length_bin`：清理后字符数 `<10` 为 `short`，`10~30` 为 `mid`，`>30` 为 `long`。
- `complexity`：固定启发式规则的结果，用于描述文本表面结构；不要与
  `quality_task_difficulty` 混用。
- `formality`：固定启发式规则的结果，仅描述表达风格，不代表内容质量。

## 5. 使用时最容易误解的几点

1. 多轮样本的正式输入是 `text`，不是单独的 `target_query`。
2. `cluster_prob` 是聚类成员置信度，不是标签准确率；质量分也不由它直接决定。
3. `frequency` 是源数据传入的信号，具体业务口径需由源数据生产方说明。
4. `complexity` 是文本规则特征；`quality_classification_difficulty` 是分类难度；
   `quality_task_difficulty` 是 Agent 执行难度，三者不能互换。
5. 最终集中的四个第一阶段质量字段按准入规则应固定为
   `exact/self_contained/clear/valid`。这些字段主要用于交付契约和审计。
6. P0、P1、P2 之间不做跨集合去重，同一源样本可能出现在多个集合中。
7. `session_id` 不是稳定的全局样本主键。需要逐条审计时，应结合文件行号和内部
   `export_provenance.jsonl` 使用。
8. 质量字段来自 LLM-as-judge 自动评估，不等同于人工金标结果。
9. `raw_text`、`text`、`context_text`、`target_query` 和 `session_id` 可能包含原始业务
   内容或标识；向项目范围外共享前应按数据治理要求脱敏。

## 6. 数据约束摘要

- 28 个字段全部必须存在，字段名与顺序固定。
- 所有字段都不能为 JSON `null`。
- 只有 `context_text` 可以是空字符串。
- `norm_freq` 和 relevance 字段在交付时保留 4 位小数。
- `quality_issue_types` 必须是非空数组，不能有重复值。
- 文件中的每一行都应能独立解析为一个 JSON 对象。

内部的 `screened.canonical.jsonl` 是流水线权威成品，包含嵌套结构、任务 ID 和一致性
信息；它不是当前下游交付契约。对外交换应以本文件说明的三份 labeled v4 为准。
