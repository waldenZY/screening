# Screening v3 当前文档

当前内部记录为 `screened_schema_version=6`，交付文件仍名为 `labeled_v5`，每条为 **14 字段**，映射契约版本为 4。旧 30 字段 v5 run 不兼容。

| 文档 | 内容 |
| --- | --- |
| [技术设计](screening_pipeline_technical_design_v3.md) | 当前链路、Stage1/Challenge/Stage2、内部记录、导出和发布复验 |
| [字段字典](screening_labeled_v5_field_dictionary.md) | 14 字段顺序、类型、语义及 `executable` 口径 |
| [配置说明](screening_pipeline_configuration_v3.md) | 启动参数、配置和运行契约 |
| [P2 自适应采样](screening_v3_p2_adaptive.md) | P2 来源、波次、配额及补采 |

历史 30 字段说明见 [归档目录](../archive/v3_history/docs/)。开发测试位于项目外部 [tests](../../../tests/)。
