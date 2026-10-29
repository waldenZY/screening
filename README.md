# Screening v4

从 cluster JSONL 和 taxonomy 构建 P0/P1/P2 数据集。当前交付是 **14 字段 `labeled_v8`**，具体字段见 [字段字典](artifacts/docs/screening_labeled_v8_field_dictionary.md)。`executable=0` 只标记，样本仍交付。启动命令与过去一致；历史 v5/v6/v7 交付对应的旧 run 不能按当前契约续跑。

当前产品版本为 **v4**；`labeled_v8` 是交付数据格式版本，两者独立。改造前的方案见 [V3 归档](artifacts/archive/v3/README.md)。旧 Prompt 备份和对照页只保存在归档中，当前 Prompt 目录只保留三份默认加载的 Prompt；可选 Stage2 的 Prompt 随隔离模块保存。

## 链路

1. 规范化源数据，构建固定候选索引和采样特征。
2. 公共索引与冻结输入就绪后，并发启动彼此独立的 P0、P1、P2 分支；各分支在 Stage1 前完成集合内去重。
3. Stage1 判断准入；P0 随即冻结成员，P1 按冻结 L2 配额补采，P2 经 Challenge 判断并在预算内自适应补采。
4. 每个分支冻结成员后即可导出；Stage2 默认关闭，显式启用时只在独立附加文件中保存难度和语义障碍，不改变正式交付字段。
5. 用冻结风险词表、会话和 Stage1 策略结论计算 `executable`，映射为 14 字段交付文件。
6. 各分支独立生成报告、复验并按需事务发布；某分支失败不撤销或阻塞其他分支。所有分支终态后再生成总报告。

Stage1 分为两次独立请求：纯会话的评测目标、澄清点与必要依赖识别，以及会话与候选类别核验；程序执行冻结筛选策略。dialogue 有澄清点仍走 direct，task 有必要澄清时走 clarification，两条路线均可准入；生产仍要求类别 exact。质量契约为13，不兼容旧 run 不续跑。当前版本见 [文档索引](artifacts/docs/README.md)，边界和工程细节见下列标准与技术设计。

首次了解标注规则，可先读 [Stage1 与可选标注标准](artifacts/docs/screening_quality_judgment_standards.md)：标注分工、各标签的衡量依据、程序准入条件及主要边界例。

独立检查模块的实际输出，见 [模块测试使用说明](module_tests/README.md)。

## 配置与运行

若用 Python 操作本项目，使用 `moto` 环境。先复制并填写 LLM 配置：

```powershell
Copy-Item screening/artifacts/config/llm_config.example.json `
  screening/artifacts/config/llm_config.local.json
```

在 `screening/artifacts/config/pipeline.default.json` 填写 `inputs.source`、`inputs.taxonomy`、`inputs.risk_keywords`。风险词表为必填 Excel，格式与作用见下文。默认 `publication.enabled=false`。

核对日期：2026-10-14。默认 P1 目标 **6,000**、P2 目标 **1,000**；LLM 全局并发 **10**、最大尝试轮数 **20**、请求前等待 **1 秒**；P2 总抽样预算 **20,000**、尾部预算 **1,000**、尾部单批 **250**、连续无进展上限 **2**。完整默认值见 [配置模板](artifacts/config/pipeline.default.json)，续跑已有 run 时仍须保留其原参数。

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

相同输入和兼容配置重跑，优先恢复可续跑 run，再复用已完成 run；总状态为 `failed` 的 run 必须显式指定原 `--run-id`，否则不会自动恢复。已完成分支不重复标注；同一发布目标内容未变时不重复复制；启动预检仍会单独发送一次连通性请求。分支构建或发布失败返回 `2`，成功分支的产物仍有效；启动、全量复验等整体错误返回 `1`。输入、Prompt、模型、质量/采样参数或数据契约变化需要兼容 run 或新 run。`--new-run` 显式新建；`--run-id` 指定兼容的已有 run；`--no-publish` 临时关闭发布。发布目标存在同名不同内容时默认拒绝；可换发布目录或显式加 `--replace`。参数说明见 [配置文档](artifacts/docs/screening_pipeline_configuration_v4.md)。

### 启动预检

正式索引和采样前，程序先检查四个 Prompt 的占位符，以及输出目录、run 存放目录、索引缓存目录的创建、写入、原子替换和删除能力。启用发布时，同时检查发布根目录和三个集合的实际发布子目录。目录探测使用独立临时文件并清理。

随后按配置的模型和请求参数发送一次短 LLM 请求，验证当前服务、凭证和模型可用；失败直接结束，退出码为 `1`，不进行预检重试。这次请求使用固定测试文本，不读取数据集 Query，也不检验标注质量。每次启动（包括续跑、已完成 run 的复用）均增加一次请求，不计入 Stage1/Challenge/Stage2 任务账本；成功摘要记录在 manifest 的 `preflight` 中。

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

每个 run 保存 `<run_root>/risk_keywords.json` 冻结快照及词表、规则哈希。续跑和发布复验使用冻结契约，原 Excel 内容变化需要新 run。完整格式、匹配边界与路径规则见 [配置说明：风险词表](artifacts/docs/screening_pipeline_configuration_v4.md#44-inputsrisk_keywords)。

## 产物

本次改造升级质量、配置和交付契约，必须使用新版配置与新 run；旧版通过基线标签保留，不能修改旧检查点套用新规则。原始候选索引仍按自身版本复用。

`<output-root>/_canonical_cache/` 保存候选索引；`<output-root>/runs/<run-id>/` 保存 manifest、冻结风险快照、质量任务与结果账本、P0/P1/P2 分支及 `screening_run_report.json` / `.md`。

`../../outputs_v4` 相对于配置文件所在目录解析；默认模板对应 `screening/outputs_v4/`。相同源和索引规则可复用缓存，换数据或配置则按身份新增目录，旧目录不自动删除。LLM 缓存位于每个 run 的 `quality/llm_result_cache.sqlite`，同 run 跨分支共享，新 run 不自动共享旧结果。仍需恢复、复验或发布时应保留 run 及其引用的索引缓存。详见 [输出目录说明](artifacts/docs/screening_pipeline_configuration_v4.md#45-outputsoutput_root)。

各分支的 `checkpoints/`、`branch_report.json`、`screened.canonical.jsonl` 和发布事务彼此独立；`*_labeled.jsonl` 是 14 字段交付视图。`export_provenance.jsonl` 将源、内部和交付逐条哈希关联。正式发布沿用以下 v8 文件名：

```text
P0-冒烟集/P0_smoke_set_v8_labeled.jsonl
P1-基准集/P1_benchmark_set_v8_labeled.jsonl
P2-对抗集/P2_adversarial_set_v8_labeled.jsonl
```

启用发布后，各分支完成导出和分支复验即可复制到 `publish_root`；全部成功后另做全量复验。不存在则发布，同哈希则复用，不同内容默认拒绝。其他分支失败或最终全量复验失败不会撤销已独立发布的文件；具体状态与事务范围见 [发布说明](artifacts/docs/screening_pipeline_configuration_v4.md#8-publication发布参数)。

完整文档入口见 [文档索引](artifacts/docs/README.md)。工程验收位于 `module_tests/tests/`，使用 `moto`、临时数据与模拟 LLM；真实参考复测单独运行。

## 可选 Stage2

默认配置为 `optional_stage2={"enabled":false,"prompt":null}`，不读取 Stage2 Prompt、不请求模型或创建空账本。可用 `--enable-stage2` 显式启用、`--disable-stage2` 关闭，详情见 [隔离模块](quality/optional_stage2/README.md)。无论开关如何，三个集合都导出相同的 labeled_v8 14字段，不再包含 quality_task_difficulty。切换开关需新 run。
