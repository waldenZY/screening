# 下游交付字段字典（downstream_labeled_v1）

核对日期：2026-10-30；labeled_mapping_version=10。每条UTF-8 JSONL恰好29个顶层字段，27个业务字段及stage1、provenance两个对象。字段顺序固定，下表为导出顺序。P0/P1第15字段为relevance_p0，P2为relevance_p2。文件名中的v4是下游固定名称，不表示恢复历史数据契约。

|序号|字段|类型与来源|
|---:|---|---|
|1|text|string，规范化上下文和清洗目标轮以 ` \| ` 拼接，实际评测输入|
|2|raw_text|string，源cluster的原始text，保留原始空白供追溯|
|3|context_text|string，前序有效轮次拼接，单轮为空|
|4|target_query|string，最后有效轮移除当前规则定义的多模态标记|
|5|turn_count|integer，前序轮数+1|
|6|sample_type|single_turn / multi_turn|
|7|L1|string，taxonomy一级类别|
|8|L2|string，taxonomy二级类别|
|9|cluster_id|integer，源cluster|
|10|cluster_prob|number [0,1]，源概率，缺失按0|
|11|prob_band|high / mid / boundary，公共概率桶|
|12|frequency|非负integer，源频次，缺失按1|
|13|freq_band|hot / warm / cold，完整源cluster内频次分档|
|14|norm_freq|number [0,1]，簇内log1p min-max；全相同取1；交付round 4位|
|15|relevance_p0 / relevance_p2|number [0,1]，Stage1集合评分，交付round 4位|
|16|length_bin|short / mid / long，清洗完整文本长度<10、10～30、>30|
|17|complexity|simple / moderate / complex，原有规则复杂度，不是质量或模型难度|
|18|formality|formal / mixed / colloquial，恢复删减前历史启发式规则|
|19|session_id|string，源会话标识，不是全局样本主键|
|20|set|P0_smoke / P1_benchmark / P2_adversarial|
|21|quality_category_fit|exact / partial / mismatch，直接映射准入类别核验；生产成员为exact|
|22|quality_validity|valid / borderline / invalid，新的Stage2质量标注|
|23|quality_intent_clarity|clear / vague / no_intent，新的Stage2质量标注|
|24|quality_issue_types|非空无重复string数组，原10项枚举；无问题仅["none"]|
|25|quality_overall_score|integer 1～5，按质量标准版本1分级|
|26|intent_label|task / qa，仅映射准入类型：task→task、dialogue→qa|
|27|executable|integer 0/1，沿用现有直接测试标记，0也交付|
|28|stage1|object，见下表；冻结判断直接透传|
|29|provenance|object，运行、来源、标注任务及质量标准版本|

## Stage1透传对象

stage1.admission恰好包含features、category、policy、screening：
- features：intent_label、intent_reason、open_points、dependencies。
- category：status、fit；生产为reviewed/exact。
- policy：完整冻结策略version、allowed_routes、require_category_exact、runtime_capabilities。
- screening：route、admitted、reasons、policy_sha256，可按冻结策略重建。

stage1.selection包含origin（initial_wave/supplement_wave）、round_id、candidate_task_id、admission_task_id、admission_feature_task_id、admission_category_task_id、candidate_sources。P2的candidate_sources保存候选来源标签与归因；P0/P1没有额外来源时为空对象。

stage1.challenge：P2保存is_challenge、primary_challenge、challenge_types、challenge_task_id；P0/P1为null。

## provenance

固定字段：run_id、source_snapshot_id、source_line、source_identity_sha256、source_record_sha256、quality_rubric_version、quality_annotation_task_id。run_id+source_identity_sha256关联具体运行及源样本；source_line辅助定位。准入与Challenge任务ID在stage1内，可继续关联quality请求账本。

source_identity由源会话标识与原始text生成，不能仅用导出text或session_id替代。质量任务哈希绑定完整会话、候选类别和准入依据，跨run不自动复用。

## 标记和评分边界

executable=1仅当已准入、direct、单轮、目标轮超过2字符、冻结风险词未命中；否则0。它不证明实际执行成功，也不等于质量分。补标给1～4分也不改变它。

quality_issue_types保留none、category_mismatch、ambiguous_intent、context_dependent、truncated、nonsense、emotional_only、too_brief、no_evaluable_goal、multi_intent。5分必须valid+clear+[none]；低于5分必须有具体问题；1分为invalid+no_intent。完整定义及正反例见[质量标准](screening_quality_judgment_standards.md)。新旧质量分数的评价口径不同。

## 文件与验证

P0-冒烟集/P0_smoke_set_v4_labeled.jsonl；P1-基准集/P1_benchmark_set_v4_labeled.jsonl；P2-对抗集/P2_adversarial_set_v4_labeled.jsonl。不生成原v8并行文件，不恢复quality_semantic_completeness、quality_classification_difficulty或quality_task_difficulty。

全部顶层字段必须存在且非null，只有context_text允许空字符串。嵌套结构按各自契约允许null，例如P0/P1的challenge。交付与内部成品按顺序逐条映射，不因质量分或executable删行。stage1来自Stage2前的冻结产物；发布时独立核验冻结哈希、模型子任务、质量任务和导出哈希。下游需求原始记录保持原文；当前已按约定沿用screening的executable与intent_label口径。
