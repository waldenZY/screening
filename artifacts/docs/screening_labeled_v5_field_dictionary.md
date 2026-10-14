# Screening 当前交付字段字典（labeled v5）

当前 `labeled_v5` 每条记录恰好 14 个字段，顺序如下。文件名延续 v5；映射契约为 `legacy_mapping_version=4`，与历史 30 字段 v5 run 不兼容。历史说明见 [30 字段字典](../archive/v3_history/docs/screening_labeled_v5_30_field_dictionary.md)。

核对日期：2026-10-14；各契约版本见 [文档索引](README.md)，完整标注规则见 [Stage1 / Stage2 评判标准](screening_quality_judgment_standards.md)。P1 目标 6,000、P2 目标 1,000 等默认采样数量不改变这 14 个交付字段。

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
| 13 | `intent_label` | string enum | Stage2 基于完整有效任务判断：操作、服务查询、写作、总结、润色和规划归 `task`；知识问答、解释、翻译和计算归 `qa`。混合意图中的有效任务优先；无可评测目标归 `qa`。 |
| 14 | `executable` | integer enum | `1` 表示未命中下游筛选条件；`0` 表示至少命中一个。正常问答也可以是 `1`。 |

全部字段必须存在且不得为 `null`；只有 `context_text` 可以为空字符串。会话四字段必须满足上表关系。导出按内部记录顺序逐条映射，不改变 P0/P1/P2 成员数。

交付 `target_query` 使用候选解析后的清洗值；前序来自规范化文本。Stage1/Stage2 的 `conversation_turns` 从 `conversation.full_text` 生成，不能根据交付的目标轮重建模型实际输入。

## `executable` 口径

以下任一条件成立时置 `0`：目标轮命中冻结风险词表；多轮；Stage2 的 `execution_issues` 含 `no_evaluable_goal`、`attachment_dependency`、`unresolved_reference` 或 `other_unfit`；`len(target_query) <= 2`。这些条件只标记，不删除样本。集合内精确与高置信近重复剔除已在候选阶段、Stage1 前完成；最终发布检查每集合规范化文本唯一，不再以重复项改变 `executable`。普通指令含“吧”等语气词不会单独置 `0`。

风险匹配使用词表 `Sheet1` 的 `关键词`、`风险级别` 两列，对核心词和原词作子串匹配；命中任一风险级别均置 `0`。冻结词表及规则哈希属于 run 契约。发布复验从词表快照、会话和 `execution_issues` 重算 `executable`。

## 内部记录与审计

`screened.canonical.jsonl` 的 `screened_schema_version=7`。它保存交付字段的来源数据、`raw_text`、`sample_type`、`complexity`、`length_bin`、Stage1 的 `category_fit`、`semantic_completeness`、`intent_clarity`、`validity`、`blocking_issues`，Stage2 的 `task_difficulty`、`intent_label`、`execution_issues`，以及来源定位与哈希、P2 Challenge 结果、任务 ID、波次来源等技术元数据。概率档、频次档、归一化频次和相关度只在候选索引与采样中使用，不复制到最终逐条内部记录。`formality` 已从特征和索引移除。

公共质量契约为 `quality_contract_version=9`。Stage1 只接收 `sample_type`、完整有序的 `conversation_turns` 和 `candidate_category={L1,L2}`；评价目标为最后一轮，前序用于恢复和核查。`blocking_issues` 六类枚举为 `attachment_dependency`、`missing_required_input`、`unresolved_reference`、`no_evaluable_goal`、`unrecoverable_text`、`unclear_evaluation_boundary`，以固定顺序保存，禁止重复和矛盾标签。四标签通过且该数组为空才准入；borderline 不准入。该数组仅内部保存，不增加交付字段。

Stage2 仍只接收 `sample_type` 和完整有序的 `conversation_turns`，不接收类别或 Stage1 判断。前序片段也可能存在缺失、指代不明或冲突；最后一句的确认不会自动否定完整任务，明确取消或结束的内容不再累计。报告契约为 10，Stage2 语义问题复核信号分母为最终已打标成员，不代表人工真值准确率。

`execution_issues` 是内部语义原因数组；空数组表示没有这些障碍。`task_difficulty=not_applicable` 当且仅当 `execution_issues=["no_evaluable_goal"]`，此时 `intent_label=qa`；无目标不能与其他障碍并列。`attachment_dependency` 根据纯文本所表达的必要内容依赖判断，不依赖附件标记；同一个缺失附件对象不重复记入 `unresolved_reference`。正常工具、权限、实时数据或补槽需求不单独构成 `other_unfit`。内部记录不保存 `quality_consistency` 或 `execution_reasons`。

导出附带 `export_provenance.jsonl` 与 `legacy_export_report.json`，用于逐条哈希、字段和来源复验。旧契约 run 不按当前规则续跑或发布。
