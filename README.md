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

首次了解标注规则，可先读 [Stage1 / Stage2 评判标准](artifacts/docs/screening_quality_judgment_standards.md)：两阶段分工、各标签的衡量依据、程序准入条件及八组边界例。

## 配置与运行

若用 Python 操作本项目，使用 `moto` 环境。先复制并填写 LLM 配置：

```powershell
Copy-Item screening/artifacts/config/llm_config.example.json `
  screening/artifacts/config/llm_config.local.json
```

在 `screening/artifacts/config/pipeline.default.json` 填写 `inputs.source`、`inputs.taxonomy`、`inputs.risk_keywords`。风险词表为必填 Excel，格式与作用见下文。默认 `publication.enabled=false`。

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

相同配置重跑会恢复未完成或失败的分支，已完成分支不会重复标注请求或发布；启动预检仍会单独发送一次连通性请求。部分失败时 CLI 返回状态码 `2`，成功分支的产物仍有效。输入、Prompt、模型、索引或映射契约变化需要新 run。`--new-run` 显式新建；`--run-id` 指定兼容的已有 run；`--no-publish` 临时关闭发布。发布目标存在同名不同内容时默认停止；可换发布目录或显式加 `--replace`。参数说明见 [配置文档](artifacts/docs/screening_pipeline_configuration_v3.md)。

### 启动预检

正式索引和采样前，程序先检查三个 Prompt 的占位符，以及输出目录、run 存放目录、索引缓存目录的创建、写入、原子替换和删除能力。启用发布时，同时检查发布根目录和三个集合的实际发布子目录。目录探测使用独立临时文件并清理。

随后按配置的模型和请求参数发送一次短 LLM 请求，验证当前服务、凭证和模型可用；失败直接结束，退出码为 `1`，不进行预检重试。这次请求使用固定测试文本，不读取数据集 Query，也不检验三个阶段的标注质量。每次启动（包括续跑、已完成 run 的复用）均增加一次请求，不计入 Stage1/Challenge/Stage2 任务账本；成功摘要记录在 manifest 的 `preflight` 中。

日志统一以 `preflight` 开头，包含 `check`、`status`、`elapsed_ms`；目录故障还记录 `path`、`operation`、`error_type`、`errno`，LLM 故障记录可获得的 `http_status` 和 `hint`，不会打印 API Key、服务地址、响应正文或原始 Query。例如：

```text
preflight {"check":"llm","status":"failed","http_status":401,"hint":"check_api_key",...}
preflight {"check":"directory","status":"failed","path":"D:/data/delivery","operation":"replace",...}
```

已有同名发布文件记录为 `publication_targets/exists_unverified`，不会仅因文件存在而停止。导出后仍按哈希判断内容是否相同；实际冲突日志以 `publication` 开头，记录集合、目标路径及停止或事务替换动作。预检只确认当前可用性，批跑重试和发布复验继续保留。

### 风险词表 `risk_keywords`

在 pipeline 配置中填写路径，或用 `--risk-keywords` 覆盖：

```json
{
  "inputs": {
    "source": "D:/data/cluster.jsonl",
    "taxonomy": "D:/data/taxonomy.json",
    "risk_keywords": "D:/data/risk_keywords.xlsx"
  }
}
```

工作簿须包含 `Sheet1`，第一行按顺序为 `关键词`、`风险级别` 两列；每条词条的两列均为非空文本，至少有一条有效词条。例如：

| 关键词 | 风险级别 |
| --- | --- |
| 定时开关机 | 高 |
| 显示重绘刷新区域 | 低 |

这些仅为格式示例，正式词条由项目维护。空行可跳过，额外非空列、空词条、数值类型的风险级别或缺失文件会使启动失败。

程序在计算交付字段 `executable` 时，用词条的核心词及原词匹配最后一轮 `target_query`。例如“定时开关机”包含核心词“开关机”“关机”，“请关机”会命中。**任一风险级别命中均置 `executable=0`**；级别保存为元数据，当前没有高低级别阈值。样本仍保留和导出，词表不改变 Stage1 准入、Stage2 模型标签或集合成员。

每个 run 保存 `<run_root>/risk_keywords.json` 冻结快照及词表、规则哈希。续跑和发布复验使用冻结契约，原 Excel 内容变化需要新 run。完整格式、匹配边界与路径规则见 [配置说明：风险词表](artifacts/docs/screening_pipeline_configuration_v3.md#44-inputsrisk_keywords)。

## 产物

若已有 run 因去重相似度微小越界（如 `1.0000000000000002`）在最终复验失败，应用数值边界修复后，保持原输入、配置、Prompt 和模型参数，显式指定原 run：

```bash
python -m screening.run_pipeline --config "/原路径/pipeline.json" --run-id "原run-id"
```

本修复不升级数据契约，不修改旧审计或检查点；已完成的分支和 Stage2 可复用，启动预检仍会发一次短请求。无需删除 run、手改审计或加 `--new-run`。其他契约版本的 run 仍须满足原有兼容校验。

`<output-root>/_canonical_cache/` 保存候选索引；`<output-root>/runs/<run-id>/` 保存 manifest、冻结风险快照、质量任务与结果账本、P0/P1/P2 分支及 `screening_run_report.json` / `.md`。

各分支的 `checkpoints/`、`branch_report.json`、`screened.canonical.jsonl` 和发布事务彼此独立；`*_labeled.jsonl` 是 14 字段交付视图。`export_provenance.jsonl` 将源、内部和交付逐条哈希关联。正式发布沿用以下 v5 文件名：

```text
P0-冒烟集/P0_smoke_set_v5_labeled.jsonl
P1-基准集/P1_benchmark_set_v5_labeled.jsonl
P2-对抗集/P2_adversarial_set_v5_labeled.jsonl
```

完整文档入口见 [文档索引](artifacts/docs/README.md)。当前工作区未保留 `tests/`；本轮使用 `moto` 环境、项目外临时数据和模拟 LLM 验证，不调用真实模型。
