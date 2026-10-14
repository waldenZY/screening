# Screening v3 当前文档

当前内部记录为 `screened_schema_version=7`，质量契约为 9，报告契约为 10；交付文件仍名为 `labeled_v5`，每条为 **14 字段**，映射契约版本为 4。旧 run 不迁移或续跑。

| 文档 | 内容 |
| --- | --- |
| [Stage1 / Stage2 评判标准](screening_quality_judgment_standards.md) | 面向新读者的可用性、意图、难度、语义障碍标准及关键边界例 |
| [技术设计](screening_pipeline_technical_design_v3.md) | 当前链路、Stage1/Challenge/Stage2、内部记录、导出和发布复验 |
| [字段字典](screening_labeled_v5_field_dictionary.md) | 14 字段顺序、类型、语义及 `executable` 口径 |
| [配置说明](screening_pipeline_configuration_v3.md) | 启动参数、配置和运行契约；[风险词表格式与匹配规则](screening_pipeline_configuration_v3.md#44-inputsrisk_keywords) |
| [P2 自适应采样](screening_v3_p2_adaptive.md) | P2 来源、波次、配额及补采 |

历史 30 字段说明见 [归档目录](../archive/v3_history/docs/)。当前工作区未保留 `tests/`；本轮使用 `moto` 环境、项目外临时数据和模拟 LLM 验证。
