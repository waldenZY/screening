# Screening 当前交付字段字典（labeled v6）

核对日期：2026-10-28；labeled_mapping_version=7，每条恰好15字段，原14字段顺序保留并追加 stage1。文件名使用 v6，不覆盖 v5；旧 run 不迁移。

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
| 14 | `executable` | integer enum | 已准入的单轮 direct 样本且长度大于2、风险未命中时为1；其余为0。 |
| 15 | `stage1` | object | features、category、policy、screening；筛选结果可由冻结策略重建。 |

全部字段必须存在且不得为 `null`；只有 `context_text` 可以为空字符串。会话四字段必须满足上表关系。导出按内部记录顺序逐条映射，不改变 P0/P1/P2 成员数。

交付 `target_query` 使用候选解析后的清洗值；前序来自规范化文本。Stage1/Stage2 的 `conversation_turns` 从 `conversation.full_text` 生成，不能根据交付的目标轮重建模型实际输入。

## `stage1` 对象

包含 features、category、policy、screening。features 是独立文本模型观察，category 为 reviewed + exact/partial/mismatch，或独立参考流程的 not_provided + null。生产交付必须 exact。policy 为版本化策略完整快照，screening 为 route、admitted、reasons 和 policy_sha256。正式成员 admitted 必须为 true。

## executable 口径

同时满足策略准入、route=direct、single_turn、len(target_query)>2、冻结风险词未命中时为1。风险词的核心词和原词匹配规则不变，任意级别命中均置0。Stage2 的 execution_issues 不再否决此字段；intent_label 维持原判。executable 不是产品执行成功证据。

内部 delivery_labels.executable_reasons 按固定顺序保存 not_admitted、not_direct、risk_keyword、multi_turn、too_short 中的命中项，报告提供汇总。0只标记，不改变成员。

## 审计

screened_schema_version=9，quality_contract_version=11，report_schema_version=11。内部 quality.stage1 与交付 stage1 对象一致；原 Stage2 三项继续保存，独立判断不变。export_provenance 与冻结策略、子任务及逐条记录哈希共同用于复验。正式环境实例、判据和证据仍由后续测试流程提供。
