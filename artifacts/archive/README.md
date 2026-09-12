# Screening v0/v1 资料归档

此目录只保存已经退出运行链路的历史资料。归档文件不会被当前 screening 代码、
默认配置或 Prompt 加载。

## 版本边界

- `v0`：统一初筛、动态分布修复和分层回捞方案。
- `v1`：P0/P1/P2 独立策略、P1 Coverage、旧有效性审核和旧发布方案。

## 归档清单

| 版本 | 类型 | 原路径 | 归档路径 | 状态 |
| --- | --- | --- | --- | --- |
| v0 | 技术文档 | `artifacts/docs/screening_pipeline_technical_design_v0.md` | `v0/docs/screening_pipeline_technical_design_v0.md` | 历史只读 |
| v0 | Prompt | `artifacts/prompts/validity_filter_prompt_v0.md` | `v0/prompts/validity_filter_prompt_v0.md` | 历史只读 |
| v1 | 技术文档 | `artifacts/docs/screening_pipeline_technical_design_v1.md` | `v1/docs/screening_pipeline_technical_design_v1.md` | 历史只读 |
| v1 | Prompt | `artifacts/prompts/validity_filter_prompt_v1.md` | `v1/prompts/validity_filter_prompt_v1.md` | 历史只读 |
| v1 | Prompt 别名快照 | `artifacts/prompts/validity_filter_prompt.md` | `v1/prompts/validity_filter_prompt.md` | 历史只读 |
| v1 | 默认配置 | `artifacts/config/pipeline.default.json` | `v1/configs/pipeline.default.json` | 历史只读 |

历史文件保持原名和原内容。新增的复盘、统计或运行分析应放入对应版本的 `reports/`
目录，并在本清单中登记。`D:\wwc\p012build` 不属于本归档范围。
