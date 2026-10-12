# Screening v3 当前文档

当前运行使用 `config_version=5`、Stage2 `quality_contract_version=6`、
`screened_schema_version=5`，交付为 30 字段 `labeled_v5`。P0/P1/P2 的准入
遵循当前实现；候选去重统一在 Stage1 前执行，`intent_label` 和 `executable` 只增加下游筛选信息，不删减通过准入的最终成员。

| 文档 | 用途 |
| --- | --- |
| [技术方案](screening_pipeline_technical_design_v3.md) | 数据链路、阶段契约、恢复和发布 |
| [配置使用手册](screening_pipeline_configuration_v3.md) | 配置字段、CLI 覆盖与 run identity |
| [labeled v5 字段字典](screening_labeled_v5_field_dictionary.md) | 30 字段顺序、取值及下游标签含义 |
| [P2 Adaptive 技术方案](screening_v3_p2_adaptive.md) | 已实施的 P2 采样与 Challenge 细节 |

实际默认值以 [pipeline.default.json](../config/pipeline.default.json) 为准；Stage1、
Challenge、Stage2 的判断依据分别以 [quality_stage1.md](../prompts/quality_stage1.md)、
[quality_challenge.md](../prompts/quality_challenge.md)、
[quality_stage2.md](../prompts/quality_stage2.md) 为准。

旧版字段说明和未实施方案已移至 [历史归档](../archive/README.md)，不作为当前运行口径。
