# Screening v3

从 cluster JSONL 和 taxonomy 构建 P0/P1/P2 数据集。当前交付是 **14 字段 `labeled_v5`**，具体字段见 [字段字典](artifacts/docs/screening_labeled_v5_field_dictionary.md)。`executable=0` 只标记，样本仍交付。启动命令与过去一致；历史 30 字段 v5 run 不能按当前契约续跑。

## 链路

1. 规范化源数据，构建固定候选索引和采样特征。
2. 公共索引与冻结输入就绪后，并发启动彼此独立的 P0、P1、P2 分支；各分支在 Stage1 前完成集合内去重。
3. Stage1 判断准入；P0 随即冻结成员，P1 按冻结 L2 配额补采，P2 经 Challenge 判断并在预算内自适应补采。
4. 每个分支一旦冻结成员便独立进入 Stage2。LLM 只接收 `sample_type` 和完整有序的 `conversation_turns`，独立输出 `task_difficulty`、`intent_label`、`execution_issues`，只添加标签。
5. 用冻结风险词表、会话和 Stage2 语义原因计算 `executable`，映射为 14 字段交付文件。
6. 各分支独立生成报告、复验并按需事务发布；某分支失败不撤销或阻塞其他分支。所有分支终态后再生成总报告。

Stage1 只接收完整轮次、样本类型和独立的候选类别，按需求可还原、关键输入可恢复及评价边界明确判断；新增 `blocking_issues`，四标签通过且无阻断原因才准入。当前质量契约为 9，旧 run 不迁移或续跑。Stage2、Challenge 与 P0/P1/P2 数量规则保持现状。详情见 [技术设计](artifacts/docs/screening_pipeline_technical_design_v3.md)。

## 配置与运行

若用 Python 操作本项目，使用 `moto` 环境。先复制并填写 LLM 配置：

```powershell
Copy-Item screening/artifacts/config/llm_config.example.json `
  screening/artifacts/config/llm_config.local.json
```

在 `screening/artifacts/config/pipeline.default.json` 填写 `inputs.source`、`inputs.taxonomy`、`inputs.risk_keywords`。风险词表 Excel 的 `Sheet1` 必须含 `关键词`、`风险级别` 两列。默认 `publication.enabled=false`。

从项目根目录运行：

```powershell
python -m screening.run_pipeline
```

也可用 CLI 覆盖路径并发布：

```powershell
python -m screening.run_pipeline `
  --source D:/data/cluster.jsonl `
  --taxonomy D:/data/taxonomy.json `
  --risk-keywords D:/data/risk_keywords.xlsx `
  --output-root D:/data/screening-runs `
  --publish-root D:/data/delivery
```

相同配置重跑会恢复未完成或失败的分支，已完成分支不会重复请求或发布。部分失败时 CLI 返回状态码 `2`，成功分支的产物仍有效。输入、Prompt、模型、索引或映射契约变化需要新 run。`--new-run` 显式新建；`--run-id` 指定兼容的已有 run；`--no-publish` 临时关闭发布。发布目标存在同名不同内容时默认停止；可换发布目录或显式加 `--replace`。参数说明见 [配置文档](artifacts/docs/screening_pipeline_configuration_v3.md)。

## 产物

`<output-root>/_canonical_cache/` 保存候选索引；`<output-root>/runs/<run-id>/` 保存 manifest、冻结风险快照、质量任务与结果账本、P0/P1/P2 分支及 `screening_run_report.json` / `.md`。

各分支的 `checkpoints/`、`branch_report.json`、`screened.canonical.jsonl` 和发布事务彼此独立；`*_labeled.jsonl` 是 14 字段交付视图。`export_provenance.jsonl` 将源、内部和交付逐条哈希关联。正式发布沿用以下 v5 文件名：

```text
P0-冒烟集/P0_smoke_set_v5_labeled.jsonl
P1-基准集/P1_benchmark_set_v5_labeled.jsonl
P2-对抗集/P2_adversarial_set_v5_labeled.jsonl
```

完整文档入口见 [文档索引](artifacts/docs/README.md)，开发回归测试位于项目外部 [tests](../tests/)。
