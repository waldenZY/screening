# Screening 历史资料归档

当前 14 字段交付之前的 v5 资料保存在 `v3_history/docs/screening_labeled_v5_30_field_dictionary.md` 与 `v3_history/docs/screening_pipeline_technical_design_30_field.md`。旧 v2 测试夹具保存在 `v3_history/tests/test_pipeline_v2_legacy.py.txt`，不参与当前回归测试。当前文档见 [文档索引](../docs/README.md)。

此目录保存历史资料和未实施草案。归档文件不会被当前 screening 代码、
默认配置或 Prompt 加载。

`v3_drafts/` 保存 V3 讨论过程中未采用的草案，不代表任何已发布版本。
`v3_history/` 保存 V3 已被新交付契约替代的字段说明。当前口径见
[文档索引](../docs/README.md)。

## 版本边界

- `v0`：统一初筛、动态分布修复和分层回捞方案。
- `v1`：P0/P1/P2 独立策略、P1 Coverage、旧有效性审核和旧发布方案。
- `v2`：统一 canonical 链路、Stage1/Stage2、P1 自适应补采，以及 P2 Adaptive 前的
  P0/P2 采样方案。完整代码快照见 Git `v2` 分支和提交 `b894753`。
- `v3_history`：已被 labeled v5 替代的 V3 labeled v4 字段口径。
- `v3_drafts`：未实施或已废弃的 V3 方案草稿。

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
| v3 | labeled v4 字段字典 | `artifacts/docs/screening_labeled_v4_field_dictionary.md` | `v3_history/docs/screening_labeled_v4_field_dictionary.md` | 历史口径 |
| v3 | P0 Adaptive 讨论稿 | `artifacts/docs/screening_v3_p0_adaptive_proposal.md` | `v3_drafts/screening_v3_p0_adaptive_proposal.md` | 未实施 |

历史配置、Prompt 和正式文档保持原名与内容；归档索引及 README 快照可以调整链接，
以保证归档内导航有效。新增的复盘、统计或运行分析应放入对应版本的 `reports/` 目录，
并在本清单中登记。`D:\wwc\p012build` 不属于本归档范围。
