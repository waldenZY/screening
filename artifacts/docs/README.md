# Screening v3 当前文档

核对日期：2026-10-14。当前产品名为 Screening v3；文件名中的 v3/v5 不代表每项内部契约版本。现行版本如下，不兼容的旧契约 run 不迁移或续跑，兼容 run 仍可恢复。

| 契约 / 标识 | 当前值 |
| --- | --- |
| `config_version`（pipeline JSON） | 7 |
| `pipeline_version` / `manifest_schema_version` | 4 / 4 |
| `p012_build_config_version` | 5 |
| `canonical_schema_version` / `canonical_index_version` / `feature_rule_version` | 2 / 3 / 3 |
| `quality_contract_version` | 9 |
| `screened_schema_version` | 7 |
| `report_schema_version` | 10 |
| `legacy_mapping_version` | 4 |
| `checkpoint_schema_version` / 去重规则版本 | 1 / 1 |
| P2 算法 | `adaptive_challenge_batches_2` |

交付仍为 **14 字段 `labeled_v5`**。现行 [默认配置](../config/pipeline.default.json)：P1 目标 6,000、P2 目标 1,000；LLM 并发 10、最大尝试轮数 20；P2 总抽样预算 20,000、尾部 1,000、尾部单批 250、无进展上限 2。修改这些默认值不升级契约版本，已有 run 续跑仍须保持原参数。

| 文档 | 内容 |
| --- | --- |
| [Stage1 / Stage2 评判标准](screening_quality_judgment_standards.md) | 面向新读者的可用性、意图、难度、语义障碍标准及关键边界例 |
| [技术设计](screening_pipeline_technical_design_v3.md) | 当前链路、Stage1/Challenge/Stage2、内部记录、导出和发布复验 |
| [字段字典](screening_labeled_v5_field_dictionary.md) | 14 字段顺序、类型、语义及 `executable` 口径 |
| [配置说明](screening_pipeline_configuration_v3.md) | 启动参数、配置和运行契约；[风险词表格式与匹配规则](screening_pipeline_configuration_v3.md#44-inputsrisk_keywords) |
| [P2 自适应采样](screening_v3_p2_adaptive.md) | P2 来源、波次、配额及补采 |

历史说明位于 `artifacts/archive/`，包括 [30 字段文档](../archive/v3_history/docs/)，不作为当前运行依据。当前工作区未保留 `tests/`；工程验收使用 `moto` 环境、项目外临时数据和模拟 LLM，不调用真实模型。
