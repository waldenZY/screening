# Screening v4 当前文档

核对日期：2026-10-30。统一阶段：预处理、Stage1采样集构建、Stage2下游交付适配。原 Stage1 改称准入评估；旧可选 Stage2 退出当前运行链路。

| 契约 / 标识 | 当前值 |
|---|---|
| config_version | 11 |
| pipeline_version / manifest_schema_version | 9 / 9 |
| p012_build_config_version | 9 |
| canonical_schema_version / canonical_index_version / feature_rule_version | 3 / 4 / 4 |
| quality_contract_version / quality_rubric_version | 14 / 1 |
| screened_schema_version / report_schema_version | 12 / 14 |
| labeled_mapping_version / labeled_schema | 10 / downstream_labeled_v1 |
| screening_policy.version | 2 |
| checkpoint_schema_version / 去重规则版本 | 1 / 1 |
| P2算法 | adaptive_challenge_batches_2 |

| 文档 | 内容 |
|---|---|
| [技术设计](screening_pipeline_technical_design_v4.md) | 三阶段边界、冻结、任务缓存与恢复 |
| [质量标准](screening_quality_judgment_standards.md) | 准入评估、四字段质量分级及边界 |
| [交付字段字典](screening_downstream_labeled_v1_field_dictionary.md) | 29字段、来源、透传对象及追溯 |
| [配置说明](screening_pipeline_configuration_v4.md) | 配置、Prompt、运行参数与发布 |
| [多数据批跑](screening_batch_runner.md) | Bash配置、只读检查、顺序批跑、后台任务与日志 |
| [P2自适应采样](screening_v4_p2_adaptive.md) | Stage1的P2选择与补采机制 |
| [下游需求原始记录](screening下游所需数据详情记录.md) | 下游参考材料，保持原文，含与当前 executable 口径不同的历史描述 |

默认P1目标6000、P2目标1000；LLM并发10、最大尝试轮数20。质量补标必跑且不改变成员。实际文件使用 *_v4_labeled.jsonl，v4是下游固定命名，与数据契约独立。

[改造前v4快照](../archive/v4_pre_downstream/ARCHIVE.md)保留原14字段v8文档和旧可选模块；[V3归档](../archive/v3/README.md)保留更早方案。旧 run 不按新契约续跑。工程验收使用 moto 环境的 module_tests/tests，真实模型复测单独执行。
