# Screening v4 三阶段技术设计

核对日期：2026-10-30。契约版本见[文档索引](README.md)。

## 预处理

cluster JSONL、taxonomy、风险词表经过校验，建立流式、可恢复的 SQLite 候选索引。会话按管道符拆分并规范化，最后有效轮为目标轮；模型使用保留标记的完整有序会话，交付目标轮按当前规则清洗，不可用交付目标轮反推实际模型输入。

公共特征只包含 probability_band、frequency_band、normalized_frequency、length_band、complexity、formality。频次统计以完整源 cluster 为总体，不在采样子集重算。源缺失概率/频次仍分别按0.0/1处理。formality 按 Git 删减前规则恢复，保留历史阈值及兼容行为，不替换当前已修正的清洗正则。索引不再保存集合 relevance。

## Stage1：采样集构建

集合评分由采样代码统一计算，默认p0=0.6×probability+0.4×normalized_frequency；p2=0.5×(1-probability)+0.5×(1-normalized_frequency)。不提前舍入。

公共输入就绪后P0/P1/P2独立并发。各分支先集合内精确与近重复去重，再做准入评估；跨集合重叠允许。去重阈值和采样配额沿用既有版本，浮点边界容差1e-12。

准入评估有三个功能步骤：

1. admission_features 模型只看 sample_type 和 conversation_turns，输出需求类型、理由、待确定事项及必要依赖。
2. admission_category 模型独立看会话与候选L1/L2，输出类别匹配，不接收前者结果。
3. admission 程序聚合独立结果，按冻结policy生成route、admitted、reasons、policy_sha256；每次读入重算核对。

生产要求类别exact；无硬阻断时dialogue走direct，task有必要澄清走clarification，否则direct。两条路线均可准入；材料、背景、未知依赖或未约定运行时能力等阻断优先。具体规则见[标准](screening_quality_judgment_standards.md)。

P0通过准入后确定成员；P1按冻结L2配额和通过率补采；P2通过准入后做Challenge判断及预算内自适应补采。Challenge输入绑定 admission_features，八种挑战类型及既有配额不变。

最终成员先写 stage1.frozen.jsonl、stage1_freeze_report.json 和 stage1_frozen 检查点，冻结候选全部公共特征、集合评分、准入结果、采样来源及Challenge。此时无下游质量标注。检查点保存恢复后续交付所需的候选、统计、轮次和审计信息。

## Stage2：下游交付适配

quality_annotation 是必跑模型任务：输入完整有序会话、L1/L2及全部冻结准入依据，输出四个固定 quality_* 字段。Prompt和分级见[标准](screening_quality_judgment_standards.md)。结果写入 stage2/quality_annotations.jsonl；评分分布及复核信号写入 quality_report.json 和主报告。

低分与跨阶段分歧不改变成员、准入结论、intent_label或executable。非法响应和传输失败按重试策略处理；仍失败则该分支无正式交付，不填假分数。冻结后失败续跑直接恢复Stage1，不再次执行采样、准入或Challenge。

映射生成27个业务字段和stage1、provenance对象。intent_label仍由准入类型唯一映射；executable仍由准入、direct、单轮、目标长度和风险词规则计算，仅作标记。quality_category_fit来自类别核验；四字段质量来自补标。正式文件名固定v4，数据契约为downstream_labeled_v1；见[字典](screening_downstream_labeled_v1_field_dictionary.md)。

## 任务、缓存与复验

质量任务使用 admission_features、admission_category、admission、challenge、quality_annotation 功能名。请求共享公平并发门、缓存、单次在途合并与重试。准入两个请求独立缓存；任一失败不丢另一成功结果。quality_annotation缓存键绑定完整实际输入（包括准入结果与策略）、Prompt和模型选项。

每个run冻结四份Prompt、输入快照、风险词表、策略和去重依据；变更影响契约的参数需新run，不跨run复用Stage1。旧配置和旧run明确拒绝，不修改其检查点。

分支复验核对Stage1冻结哈希及逐条成员、独立准入子任务、Challenge、补标任务输入/结果、字段来源和逐条导出哈希。正式交付逐条透传的子任务ID与请求账本绑定。全量复验再校验汇总报告及守恒关系。

各分支Stage2完成即可独立事务发布；其他分支失败不会撤销成功分支。全部成功后做全量复验。当前只保留完整链路入口；预处理、Stage1、Stage2是职责和状态边界，不新增阶段CLI。
