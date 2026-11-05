# 下游交付字段字典（downstream_labeled_v2）

labeled_mapping_version=11。文件名保留v4，schema与文件名独立。所有文件为UTF-8 JSONL。P0/P1基础版22字段、标注版30字段；P2基础版23字段、标注版31字段。

## 基础字段及顺序

|序号|字段|类型与来源|
|---:|---|---|
|1|text|string，规范化上下文和清洗目标轮以 ` \| ` 拼接，实际评测输入|
|2|raw_text|string，源cluster原始text，保留原始空白|
|3|context_text|string，前序有效轮拼接，单轮为空|
|4|target_query|string，最后有效轮移除现有规则的多模态标记|
|5|turn_count|integer，有效轮数|
|6|sample_type|single_turn / multi_turn|
|7|L1|string，taxonomy一级类别|
|8|L2|string，taxonomy二级类别|
|9|cluster_id|integer，源簇ID|
|10|cluster_prob|number [0,1]，缺失按0|
|11|prob_band|high / mid / boundary，沿用概率桶|
|12|frequency|非负integer，源频次缺失按1|
|13|freq_band|hot / warm / cold，完整源cluster内频次档|
|14|norm_freq|number [0,1]，簇内log1p min-max，全同取1，交付四位小数|
|15|relevance_p0|number [0,1]，所有集合提供，交付四位小数|
|仅P2追加|relevance_p2|number [0,1]，紧随relevance_p0，交付四位小数|
|16|length_bin|short / mid / long，现有长度档|
|17|complexity|simple / moderate / complex，现有规则复杂度|
|18|formality|formal / mixed / colloquial，现有启发式正式度|
|19|session_id|string，源会话ID，不是全局样本主键|
|20|set|P0_smoke / P1_benchmark / P2_adversarial|
|21|intent_label|task / qa，Stage1的task→task、dialogue→qa|
|22|executable|integer 0/1，已准入、direct、单轮、目标轮超过2字符且风险词未命中为1|

## labeled逐行追加的八字段（顺序固定）

|字段|取值与来源|
|---|---|
|quality_category_fit|exact / partial / mismatch，直接复用Stage1类别；生产为exact|
|quality_semantic_completeness|self_contained / context_dependent，direct/clarification分别映射，不输出fragments|
|quality_intent_clarity|clear / vague / no_intent，现有Stage2 LLM|
|quality_validity|valid / borderline / invalid，现有Stage2 LLM|
|quality_classification_difficulty|trivial / moderate / hard，prob_band的high/mid/boundary分别映射|
|quality_task_difficulty|simple / moderate / complex，直接映射complexity|
|quality_issue_types|现有Stage2 LLM；非空无重复数组，none独占；保留no_evaluable_goal等十项枚举|
|quality_overall_score|1～5整数，现有Stage2 LLM评分标准|

分类难度是聚类概率档代理，任务难度是规则复杂度代理，均不声称独立模型判定。语义完整性的context_dependent表示需澄清，与问题类型同名值不同，不要求联动。invalid仍可为2分，不恢复旧04的自动清分或not_applicable规则。

基础版与labeled同成员、同顺序，基础字段逐字段一致；不按质量或executable删行。全部字段非null，只有context_text允许空字符串。保留text的完整多轮语义，不用target_query替代。

## 追溯与文件

外部不透传stage1/provenance对象。内部export_provenance记录每行输出索引、源身份、基础版/标注版/内部记录哈希及模型任务关联；Stage1冻结结果保留完整准入、选择和Challenge依据。

P0-冒烟集/P0_smoke_set_v4.jsonl、P0_smoke_set_v4_labeled.jsonl；P1-基准集/P1_benchmark_set_v4.jsonl、P1_benchmark_set_v4_labeled.jsonl；P2-对抗集/P2_adversarial_set_v4.jsonl、P2_adversarial_set_v4_labeled.jsonl。

采样报告按基础版统计；质量报告按labeled统计。每集合十个切片保留labeled原行及顺序，允许重叠，零条生成空文件。可视化仅创建空目录。
