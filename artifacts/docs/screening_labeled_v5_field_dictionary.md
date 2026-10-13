# Screening 当前交付字段字典（labeled v5）

当前 `labeled_v5` 每条记录恰好 14 个字段，顺序如下。文件名延续 v5；映射契约为 `legacy_mapping_version=4`，与历史 30 字段 v5 run 不兼容。历史说明见 [30 字段字典](../archive/v3_history/docs/screening_labeled_v5_30_field_dictionary.md)。

| 序号 | 字段 | 类型 | 含义 |
| ---: | --- | --- | --- |
| 1 | `text` | string | 实际测试输入。单轮等于 `target_query`；多轮为前序轮次与目标轮以 ` | ` 连接。 |
| 2 | `context_text` | string | 前序轮次以 ` | ` 连接；单轮为空字符串。 |
| 3 | `target_query` | string | 最后一轮用户输入。 |
| 4 | `turn_count` | integer | 会话轮数，至少为 1。 |
| 5 | `L1` | string | 一级语义类别。 |
| 6 | `L2` | string | 二级语义类别。 |
| 7 | `cluster_id` | integer | 源数据簇 ID。 |
| 8 | `cluster_prob` | number | 源数据簇概率，范围 0～1；缺失时按候选索引规则补默认值。 |
| 9 | `frequency` | integer | 源数据频次，非负；缺失时按候选索引规则补默认值。 |
| 10 | `session_id` | string | 源会话 ID。 |
| 11 | `set` | string enum | `P0_smoke`、`P1_benchmark`、`P2_adversarial`。 |
| 12 | `quality_task_difficulty` | string enum | Stage2 任务难度：`atomic`、`simple`、`moderate`、`complex`、`extreme`、`not_applicable`。 |
| 13 | `intent_label` | string enum | Stage2 基于完整会话主要意图判断：`task` 或 `qa`。无可评测目标归 `qa`。 |
| 14 | `executable` | integer enum | `1` 表示未命中下游筛选条件；`0` 表示至少命中一个。正常问答也可以是 `1`。 |

全部字段必须存在且不得为 `null`；只有 `context_text` 可以为空字符串。会话四字段必须满足上表关系。导出按内部记录顺序逐条映射，不改变 P0/P1/P2 成员数。

## `executable` 口径

以下任一条件成立时置 `0`：目标轮命中冻结风险词表；多轮；Stage2 的 `execution_issues` 含 `no_evaluable_goal`、`attachment_dependency`、`unresolved_reference` 或 `other_unfit`；`len(target_query) <= 2`。这些条件只标记，不删除样本。集合内精确与高置信近重复剔除已在候选阶段、Stage1 前完成；最终发布检查每集合规范化文本唯一，不再以重复项改变 `executable`。普通指令含“吧”等语气词不会单独置 `0`。

风险匹配使用词表 `Sheet1` 的 `关键词`、`风险级别` 两列，对核心词和原词作子串匹配；命中任一风险级别均置 `0`。冻结词表及规则哈希属于 run 契约。发布复验从词表快照、会话和 `execution_issues` 重算 `executable`。

## 内部记录与审计

`screened.canonical.jsonl` 的 `screened_schema_version=6`。它保存交付字段的来源数据、`raw_text`、`sample_type`、`complexity`、`length_bin`、Stage1 的 `category_fit`、`semantic_completeness`、`intent_clarity`、`validity`，Stage2 的 `task_difficulty`、`intent_label`、`execution_issues`，以及来源定位与哈希、P2 Challenge 结果、任务 ID、波次来源等技术元数据。概率档、频次档、归一化频次和相关度只在候选索引与采样中使用，不复制到最终逐条内部记录。`formality` 已从特征和索引移除。

`execution_issues` 是内部语义原因数组；空数组表示没有这些障碍。`no_evaluable_goal` 要求 `intent_label=qa`。内部记录不保存 `quality_consistency` 或 `execution_reasons`。

导出附带 `export_provenance.jsonl` 与 `legacy_export_report.json`，用于逐条哈希、字段和来源复验。旧契约 run 不按当前规则续跑或发布。
