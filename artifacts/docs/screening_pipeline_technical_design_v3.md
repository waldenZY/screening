# Screening v3 当前技术设计

本文件描述当前链路契约。历史 30 字段设计已归档于 [旧版设计](../archive/v3_history/docs/screening_pipeline_technical_design_30_field.md)。启动命令与配置方式见 [README](../../README.md) 和 [配置说明](screening_pipeline_configuration_v3.md)。

## 1. 数据流与成员决定

源 JSONL 与分类映射构成固定候选索引。索引解析会话、校验分类和源信号，计算采样所需的概率档、频次档、归一化频次、相关度、长度档和复杂度。`formality` 不再计算或进入索引。源索引、特征规则和去重规则有独立版本及哈希；旧规则 run 不能按当前契约续跑。

公共索引和冻结输入完成后，P0 冒烟、P1 基准和 P2 对抗集合由三个 branch runner 并发构建。P0 经 Stage1 后立即冻结成员并进入 Stage2；P1 在冻结 L2 配额下补采后进入 Stage2；P2 自适应取候选，经 Stage1 与 Challenge 判定并在预算内补采后进入 Stage2。三个分支拥有独立检查点、账本、产物和发布事务。一个分支变慢或失败不会取消、回滚或阻塞其他分支。具体采样配额、波次及停止条件见 [P2 自适应说明](screening_v3_p2_adaptive.md) 与配置。

集合内文本去重统一在候选阶段、Stage1 调用前完成，覆盖初采和补采。规范化仅去空白与标点；规范化相同的文本直接去重。近重复先以双字 Jaccard ≥ 0.45 找到待比较对，再要求 L2 相同、源索引冻结的字符 2/3-gram IDF 加权 Jaccard ≥ 0.80、规范化编辑相似度 ≥ 0.90 才自动剔除。边界对保留并审计。不同集合允许成员重叠，跨集合 overlap 只进入总报告。最终发布再检查每集合规范化文本唯一。

三个分支共享一个公平 LLM 请求门。总在途请求不超过 `quality.workers`，等待分支按轮转取得请求位，空闲额度可由其他分支使用。同一缓存键并发出现时只发送一次请求；缓存、任务账本和结果账本的写入加锁提交。

## 2. 判断阶段

Stage1 严格 JSON 输出四项：`category_fit`、`semantic_completeness`、`intent_clarity`、`validity`。既有准入标准不变。P2 Challenge 严格 JSON 输出 `is_challenge`、`primary_challenge`、`challenge_types`；它只决定 P2 候选是否满足挑战条件。

成员确定后，Stage2 仅对最终成员独立判断，严格 JSON 只输出：

```json
{
  "task_difficulty": "atomic",
  "intent_label": "task",
  "execution_issues": []
}
```

`task_difficulty` 可取 `atomic`、`simple`、`moderate`、`complex`、`extreme`、`not_applicable`。`intent_label` 基于完整会话的主要意图区分 `task` 与 `qa`；纯语气或情绪且无可评测目标归 `qa`。`execution_issues` 只能包含 `no_evaluable_goal`、`attachment_dependency`、`unresolved_reference`、`other_unfit`，无障碍时为空数组；`no_evaluable_goal` 要求 `intent_label=qa`。判断细则以 [当前 Stage2 Prompt](../prompts/quality_stage2.md) 为准。

Stage2 不筛掉成员。其结果进入 `quality/stage2_results.jsonl` 账本及内部逐条记录。任务键和缓存键仍受 Prompt、模型参数、会话及 Stage1 结果约束，以便断点续跑和相同调用复用。旧跨阶段一致率及 `stage2_consistency.jsonl` 已移除。

## 3. 下游标签

`intent_label` 直接来自 Stage2。`executable` 为整数 `0/1`，正常问答也可以为 `1`。目标轮命中冻结风险词表、多轮、目标轮长度 ≤ 2，或 Stage2 有任一 `execution_issues`，都会使其为 `0`；样本仍保留。只按风险词表做风险动作匹配，不增加 Stage2 风险推测。集合内近重复由候选阶段剔除，最终不再存在 `duplicate_text` 原因。

风险词表为必填 Excel，`Sheet1` 包含 `关键词`、`风险级别`。匹配规则先按既定前后缀与特殊映射提取核心词，再用核心词及原词对子串匹配；词表中任何风险级别命中均置 `0`。原表内容哈希和规范化规则快照进入 run 契约，发布复验使用冻结快照。词表缺失或格式错误使新 run 停止。

## 4. 内部记录、交付与报告

每集合的 `screened.canonical.jsonl` 为内部权威记录，`screened_schema_version=6`。逐条记录包含 `set`、候选来源/分类/会话与必要信号、`raw_text`、`sample_type`、`length_bin`、`complexity`、Stage1 四项、Stage2 三项、`delivery_labels` 两项、P2 Challenge、任务关联 ID 和波次元数据。候选阶段的概率档、频次档、归一化频次、相关度不复制到最终内部记录；内部不保存 `quality_consistency` 或 `execution_reasons`。

最终交付仍为 `labeled_v5`、沿用 v5 文件名，但映射契约提升为 `legacy_mapping_version=4`，每条恰好 [14 个固定字段](screening_labeled_v5_field_dictionary.md)。P0/P1/P2 分别写 `P0_smoke_set_v5_labeled.jsonl`、`P1_benchmark_set_v5_labeled.jsonl`、`P2_adversarial_set_v5_labeled.jsonl`。导出依内部记录稳定顺序逐条映射，不删 `executable=0` 的样本。内部 `export_provenance.jsonl` 记录逐条源、内部和导出哈希，`legacy_export_report.json` 记录字段检查。

每个成功分支生成 `branch_report.json`，可独立校验和发布。所有分支进入终态后生成总报告；部分失败时写出成功分支、失败原因、缺失分支和成功分支间 overlap。`screening_run_report.json` / `.md` 报告 Stage1、Challenge、P1/P2 波次、配额、P2 预算、任务难度、`intent_label`、`executable` 与成员数。报告契约为 `report_schema_version=8`。

## 5. 发布复验与兼容性

发布前独立核对当前 run 契约、源和索引、Stage1/Challenge/Stage2 结果账本、最终成员任务、内部记录、每集合文本唯一、14 字段顺序/类型/会话关系、内部与导出逐条一致性、报告统计及产物哈希。`executable` 从冻结词表、会话和 `execution_issues` 重新计算，不能只信任内部存值。交付记录数必须等于最终成员数。

manifest 分别记录 P0/P1/P2 的阶段、耗时、错误、产物和发布状态。全部成功时总状态为 `completed` 或 `published`；部分失败为 `partial_failed`；全部失败为 `failed`。当前并发契约不续跑旧 pipeline/build/manifest/report 版本。发布目标若已有同名不同内容，默认停止；使用新目录或显式替换。测试位于项目外部 `tests/`，包括并发上限、公平调度、分支故障隔离、断点续跑与篡改阻止发布。
