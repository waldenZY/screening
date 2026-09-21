# Screening v2 归档

此目录冻结 P2 Adaptive / Challenge Judge 引入前的 Screening v2 文档、配置和 Prompt。

## 完整代码快照

- Git 分支：`v2`
- Git 提交：`b894753`（文档整理）

完整 V2 源代码以该分支和提交为准，本目录不重复复制 Python 源码。

## 归档内容

- `configs/`：V2 默认 Pipeline 与 LLM 示例配置；
- `docs/`：V2 使用说明、技术方案、配置手册、字段字典、P1 补采专题及版本对比；
- `prompts/`：V2 Stage1、Stage2 Prompt；
- `reports/`：脱敏 V2 运行报告的预留目录。

V2 的主要边界是旧 P0/P2 采样、Stage1/Stage2 两阶段质量处理和 P1 自适应补采。
目录结构与 V0/V1 统一按资料类型组织。归档文档中的导航链接已改为归档内路径；未经
整理的原始文件和完整代码仍以 Git `v2` 分支及上述提交为准。当前 V3 不会加载本目录
中的配置或 Prompt，也不会恢复 `pipeline_version=2` 的 run。
