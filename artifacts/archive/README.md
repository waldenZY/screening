# Screening v0/v1/v2 资料归档

此目录只保存已经退出运行链路的历史资料。归档文件不会被当前 screening 代码、
默认配置或 Prompt 加载。

`v3_drafts/` 保存 V3 讨论过程中未采用的草案，不代表任何已发布版本。

## 版本边界

- `v0`：统一初筛、动态分布修复和分层回捞方案。
- `v1`：P0/P1/P2 独立策略、P1 Coverage、旧有效性审核和旧发布方案。
- `v2`：统一 canonical 链路、Stage1/Stage2、P1 自适应补采，以及 P2 Adaptive 前的
  P0/P2 采样方案。完整代码快照见 Git `v2` 分支和提交 `b894753`。

## 归档清单

| 版本 | 类型 | 原路径 | 归档路径 | 状态 |
| --- | --- | --- | --- | --- |
| v0 | 技术文档 | `artifacts/docs/screening_pipeline_technical_design_v0.md` | `v0/docs/screening_pipeline_technical_design_v0.md` | 历史只读 |
| v0 | Prompt | `artifacts/prompts/validity_filter_prompt_v0.md` | `v0/prompts/validity_filter_prompt_v0.md` | 历史只读 |
| v1 | 技术文档 | `artifacts/docs/screening_pipeline_technical_design_v1.md` | `v1/docs/screening_pipeline_technical_design_v1.md` | 历史只读 |
| v1 | Prompt | `artifacts/prompts/validity_filter_prompt_v1.md` | `v1/prompts/validity_filter_prompt_v1.md` | 历史只读 |
| v1 | Prompt 别名快照 | `artifacts/prompts/validity_filter_prompt.md` | `v1/prompts/validity_filter_prompt.md` | 历史只读 |
| v1 | 默认配置 | `artifacts/config/pipeline.default.json` | `v1/configs/pipeline.default.json` | 历史只读 |
| v2 | 归档说明与代码快照 | Git `v2` / `b894753` | `v2/README.md` | 历史只读 |
| v2 | 文档、配置、Prompt | V2 工作区 | `v2/docs/`、`v2/configs/`、`v2/prompts/` | 历史只读 |

历史配置、Prompt 和正式文档保持原名与内容；归档索引及 README 快照可以调整链接，
以保证归档内导航有效。新增的复盘、统计或运行分析应放入对应版本的 `reports/` 目录，
并在本清单中登记。`D:\wwc\p012build` 不属于本归档范围。
