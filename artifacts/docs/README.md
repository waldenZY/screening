# Screening v4 当前文档

核对日期：2026-10-29。当前产品版本 v4 与交付格式 labeled_v8 独立于各项内部契约版本。现行版本如下，不兼容的旧契约 run 不迁移或续跑，兼容 run 仍可恢复。

| 契约 / 标识 | 当前值 |
| --- | --- |
| `config_version`（pipeline JSON） | 10 |
| `pipeline_version` / `manifest_schema_version` | 8 / 8 |
| `p012_build_config_version` | 8 |
| `canonical_schema_version` / `canonical_index_version` / `feature_rule_version` | 2 / 3 / 3 |
| `quality_contract_version` | 13 |
| `screened_schema_version` | 11 |
| `report_schema_version` | 13 |
| `labeled_mapping_version` | 9 |
| `screening_policy.version` | 2 |
| `checkpoint_schema_version` / 去重规则版本 | 1 / 1 |
| P2 算法 | `adaptive_challenge_batches_2` |

当前交付为 **14 字段 `labeled_v8`**。现行 [默认配置](../config/pipeline.default.json)：P1 目标 6,000、P2 目标 1,000；LLM 并发 10、最大尝试轮数 20；P2 总抽样预算 20,000、尾部 1,000、尾部单批 250、无进展上限 2。修改这些默认值不升级契约版本，已有 run 续跑仍须保持原参数。

| 文档 | 内容 |
| --- | --- |
| [Stage1 与可选标注标准](screening_quality_judgment_standards.md) | 面向新读者的可用性、意图、可选附加标注标准及关键边界例 |
| [技术设计](screening_pipeline_technical_design_v4.md) | 当前链路、Stage1/Challenge/Stage2、内部记录、导出和发布复验 |
| [字段字典](screening_labeled_v8_field_dictionary.md) | 14 字段顺序、类型、语义及 `executable` 口径 |
| [配置说明](screening_pipeline_configuration_v4.md) | 启动参数、配置和运行契约；[风险词表格式与匹配规则](screening_pipeline_configuration_v4.md#44-inputsrisk_keywords) |
| [P2 自适应采样](screening_v4_p2_adaptive.md) | P2 来源、波次、配额及补采 |

旧方案统一保存在 [V3 归档](../archive/v3/README.md)，完整旧源码见基线标签；归档不作为当前运行依据。工程验收位于 `module_tests/tests/`，使用 `moto`、临时数据与模拟 LLM；真实模型复测另行执行。

Stage2 默认关闭，其逻辑、Prompt、示例及测试集中于 [隔离模块](../../quality/optional_stage2/README.md)。启用时仅产出附加标注，正式交付仍为三个集合相同14字段。默认主报告不展示未评估的难度或语义复核统计。
