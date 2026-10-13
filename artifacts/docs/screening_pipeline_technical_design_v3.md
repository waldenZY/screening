# Screening v3 当前技术设计

本文件描述当前链路契约。历史 30 字段设计已归档于 [旧版设计](../archive/v3_history/docs/screening_pipeline_technical_design_30_field.md)。启动命令与配置方式见 [README](../../README.md) 和 [配置说明](screening_pipeline_configuration_v3.md)。

## 1. 数据流与成员决定

源 JSONL 与分类映射构成固定候选索引。索引解析会话、校验分类和源信号，计算采样所需的概率档、频次档、归一化频次、相关度、长度档和复杂度。`formality` 不再计算或进入索引。源索引、特征规则和去重规则有独立版本及哈希；旧规则 run 不能按当前契约续跑。

公共索引和冻结输入完成后，P0 冒烟、P1 基准和 P2 对抗集合由三个 branch runner 并发构建。P0 经 Stage1 后立即冻结成员并进入 Stage2；P1 在冻结 L2 配额下补采后进入 Stage2；P2 自适应取候选，经 Stage1 与 Challenge 判定并在预算内补采后进入 Stage2。三个分支拥有独立检查点、账本、产物和发布事务。一个分支变慢或失败不会取消、回滚或阻塞其他分支。具体采样配额、波次及停止条件见 [P2 自适应说明](screening_v3_p2_adaptive.md) 与配置。

集合内文本去重统一在候选阶段、Stage1 调用前完成，覆盖初采和补采。规范化仅去空白与标点；规范化相同的文本直接去重。近重复先以双字 Jaccard ≥ 0.45 找到待比较对，再要求 L2 相同、源索引冻结的字符 2/3-gram IDF 加权 Jaccard ≥ 0.80、规范化编辑相似度 ≥ 0.90 才自动剔除。边界对保留并审计。不同集合允许成员重叠，跨集合 overlap 只进入总报告。最终发布再检查每集合规范化文本唯一。

三个分支共享一个公平 LLM 请求门。总在途请求不超过 `quality.workers`，等待分支按轮转取得请求位，空闲额度可由其他分支使用。同一缓存键并发出现时只发送一次请求；缓存、任务账本和结果账本的写入加锁提交。

## 2. 判断阶段

Stage1 输入恰好为 `sample_type`、`conversation_turns`、`candidate_category={L1,L2}`，轮次从 canonical 的 `conversation.full_text` 按既有规则生成，不能由清洗后的目标轮重建。最后一轮为测试目标，前序仅用于理解、恢复和核查；前序有请求不能自动证明目标轮有效。类别只能核验，不能补造目标、对象或状态。调用为一条 user 消息，Prompt 唯一占位符为 `{CANDIDATE_JSON}`，样本中的标注指令不可执行。

Stage1 严格 JSON 输出五项：`category_fit`、`semantic_completeness`、`intent_clarity`、`validity`、`blocking_issues`。统一准入为 `exact + self_contained + clear + valid + blocking_issues=[]`。需求须可还原、关键输入须可恢复、响应须有明确评价边界；允许口语、多轮省略及非唯一答案。普通参数缺失由 LLM 判断能否形成可评价的正常澄清，允许 valid 或 borderline，程序不按关键词强制增删原因；borderline 不准入。

阻断原因固定为 `attachment_dependency`、`missing_required_input`、`unresolved_reference`、`no_evaluable_goal`、`unrecoverable_text`、`unclear_evaluation_boundary`，按此顺序规范化，拒绝非法或重复原因。同一缺失内容不重复归因。非空原因不能为 valid；附件/关键输入/指代缺失必须为 context_dependent；不可恢复文本必须为 fragments；无目标必须为 no_intent。矛盾响应作为契约错误重试，不静默覆盖标签。规则和正反例见 [Stage1 Prompt](../prompts/quality_stage1.md)。P2 Challenge 的输入、缓存和输出保持现状，它只决定 P2 挑战资格。

成员确定后，Stage2 仅对最终成员独立判断。输入严格为 `sample_type` 和有序纯文本数组 `conversation_turns`，从 canonical 的 `conversation.full_text` 按既有轮次规则生成；不从清洗后的 `target_query` 重建，不提供 `L1/L2`、Stage1 判断、采样信号、集合、附件或角色状态字段。Stage1 准入仍由编排器和内部记录校验保证。Stage2 严格 JSON 只输出：

```json
{
  "task_difficulty": "atomic",
  "intent_label": "task",
  "execution_issues": []
}
```

`task_difficulty` 可取 `atomic`、`simple`、`moderate`、`complex`、`extreme`、`not_applicable`，按实质推理、约束组合和步骤依赖判断。完整有效任务包括相关前序目标、补充条件和明确纠正后的要求；明确取消或结束的内容不累计，不能仅凭最后一句确认就判无目标，也不能默认前序正确或已执行。

`intent_label` 中，操作、服务查询、写作、总结、润色和规划归 `task`，知识问答、解释、翻译和计算归 `qa`；混合意图中的有效操作、服务或生成任务优先。`execution_issues` 只能包含 `no_evaluable_goal`、`attachment_dependency`、`unresolved_reference`、`other_unfit`，无障碍时为空数组。`not_applicable` 当且仅当原因数组只有 `no_evaluable_goal`，此时意图必须为 `qa`。附件依赖根据文字中的必要内容依赖判断，不要求附件标记；正常工具、权限、实时数据和补槽不单独构成障碍。判断细则以 [当前 Stage2 Prompt](../prompts/quality_stage2.md) 为准。

Stage2 不筛掉成员。其结果进入 `quality/stage2_results.jsonl` 账本及内部逐条记录。Stage1 和 Stage2 任务/缓存键均由阶段、Prompt 哈希、模型参数哈希和实际 payload 哈希决定；账本保存 `input_sha256` 而非代表样本完整 taxonomy 摘要。Stage1 的 `candidate_category_sha256` 仅计算实际输入类别，簇等非输入元数据不造成合并冲突。来源身份与行号按相同输入合并；Stage1 同类别同文本可跨分支复用，Stage2 不含类别或 Stage1 结果，可进一步跨分类复用。Challenge 的键保持现状。旧跨阶段一致率及 `stage2_consistency.jsonl` 已移除。

接口为 `run_stage2(candidates, *, branch=...)`、`task_id(stage, candidate)` 和 `validate_stage2_result(value)`，不要求 Stage1 结果参数。当前质量契约从 8 升为 9，新语义创建新 run，不迁移或续跑旧检查点。Stage2 输入及标注语义保持现状。

## 3. 下游标签

`intent_label` 直接来自 Stage2。`executable` 为整数 `0/1`，正常问答也可以为 `1`。目标轮命中冻结风险词表、多轮、目标轮长度 ≤ 2，或 Stage2 有任一 `execution_issues`，都会使其为 `0`；样本仍保留。只按风险词表做风险动作匹配，不增加 Stage2 风险推测。集合内近重复由候选阶段剔除，最终不再存在 `duplicate_text` 原因。

风险词表为必填 Excel，`Sheet1` 包含 `关键词`、`风险级别`。匹配规则先按既定前后缀与特殊映射提取核心词，再用核心词及原词对子串匹配；词表中任何风险级别命中均置 `0`。原表内容哈希和规范化规则快照进入 run 契约，发布复验使用冻结快照。词表缺失或格式错误使新 run 停止。

## 4. 内部记录、交付与报告

每集合的 `screened.canonical.jsonl` 为内部权威记录，`screened_schema_version=7`。逐条记录包含 `set`、候选来源/分类/会话与必要信号、`raw_text`、`sample_type`、`length_bin`、`complexity`、Stage1 五项、Stage2 三项、`delivery_labels` 两项、P2 Challenge、任务关联 ID 和波次元数据。候选阶段的概率档、频次档、归一化频次、相关度不复制到最终内部记录；内部不保存 `quality_consistency` 或 `execution_reasons`。

最终交付仍为 `labeled_v5`、沿用 v5 文件名，但映射契约提升为 `legacy_mapping_version=4`，每条恰好 [14 个固定字段](screening_labeled_v5_field_dictionary.md)。P0/P1/P2 分别写 `P0_smoke_set_v5_labeled.jsonl`、`P1_benchmark_set_v5_labeled.jsonl`、`P2_adversarial_set_v5_labeled.jsonl`。导出依内部记录稳定顺序逐条映射，不删 `executable=0` 的样本。内部 `export_provenance.jsonl` 记录逐条源、内部和导出哈希，`legacy_export_report.json` 记录字段检查。

每个成功分支生成 `branch_report.json`，可独立校验和发布。所有分支进入终态后生成总报告；部分失败时写出成功分支、失败原因、缺失分支和成功分支间 overlap。报告契约为 `report_schema_version=10`。全量、独立分支及部分失败报告均按分支统计 Stage1 六类阻断原因、borderline、invalid，保留原四标签拒绝组合；多原因计数允许重叠。最终成员的 Stage2 问题数量、分类及比例称为“Stage2 语义问题复核信号”，分母为该分支已完成 Stage2 的最终成员，不是人工真值准确率或误放行率。日志及报告不新增原始 Query 或自由文本理由。

## 5. 发布复验与兼容性

发布前独立核对当前 run 契约、源和索引、Stage1/Challenge/Stage2 结果账本、最终成员任务、内部记录、每集合文本唯一、14 字段顺序/类型/会话关系、内部与导出逐条一致性、报告统计及产物哈希。`executable` 从冻结词表、会话和 `execution_issues` 重新计算，不能只信任内部存值。交付记录数必须等于最终成员数。

全量与单分支发布均从最终成员重算 Stage1/Stage2 输入哈希和任务键，核对任务来源、结果及 Stage1 无阻断准入条件。分支审计按其候选身份核对 Stage1 结果，Stage2 统计按最终成员核对。单分支不要求其他分支账本全部进入终态。

固定的 [Stage1 离线样例](../evals/stage1_cases.json) 和 [28 条 Stage2 离线样例](../evals/stage2_cases.json) 覆盖标注边界，供人工核对规则和期望标签。Stage2 样例仅同步公共质量契约版本。请求捕获、缓存复用、输出约束和发布复验通过模拟 LLM 验证；这些测试不测量真实模型准确率，本轮不调用真实模型。

manifest 分别记录 P0/P1/P2 的阶段、耗时、错误、产物和发布状态。全部成功时总状态为 `completed` 或 `published`；部分失败为 `partial_failed`；全部失败为 `failed`。当前并发契约不续跑旧 pipeline/build/manifest/report 版本。发布目标若已有同名不同内容，默认停止；使用新目录或显式替换。测试位于项目外部 `tests/`，包括并发上限、公平调度、分支故障隔离、断点续跑与篡改阻止发布。
