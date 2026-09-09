# 分集合筛选与 P1 Coverage 链路

这套链路在同一个 run 中并行执行三个相互隔离的集合分支：

- P0：质检 2–5 分样本，只接纳 KEEP。
- P1：质检 3–5 分样本，按 taxonomy Population 的固定 L1 配额选择，并从 cluster 全集增量补样。
- P2：质检 3–5 分样本，接纳 KEEP 和 `TESTABLE_DIFFICULTY` REVIEW。

三个分支共享模型并发上限、Prompt 快照、日志、运行清单和最终发布门禁。P1 补样审核的是新候选，不会重新审核旧 REJECT，也不会降低统一质检标准。

实现方案文档： [v0 历史链路](artifacts/docs/screening_pipeline_technical_design_v0.md) · [v1 当前链路](artifacts/docs/screening_pipeline_technical_design_v1.md)

## 一键运行

先安装运行依赖，并复制唯一的本地 LLM 配置模板：

```powershell
python -m pip install -r filter_v2/requirements.txt
Copy-Item filter_v2/artifacts/config/llm_config.example.json `
  filter_v2/artifacts/config/llm_config.local.json
```

然后在 `llm_config.local.json` 中填写 API URL、Key、模型和固定请求参数。该文件已被 Git 忽略；所有命令只读取这一处，不读取环境变量。

然后在 `pipeline.default.json` 的 `inputs` 中填写同一个 week 对应的三项输入：

```json
"inputs": {
  "source_root": "D:/data/week1",
  "taxonomy": "D:/data/week1/taxonomy.json",
  "upstream_cluster_population": "D:/data/week1/cluster.jsonl"
}
```

建议使用绝对路径和正斜杠。三个路径必须作为一组同步切换；链路会校验文件、taxonomy 映射和 cluster 内容，但在输入本身没有共同 week 标识时，无法自动证明三个文件来自同一 week。

配置完成后直接运行：

```powershell
python -m filter_v2.run_pipeline
```

只处理 P0/P2 时不需要两个 P1 外部输入：

```powershell
python -m filter_v2.run_pipeline --sets P0,P2
```

`--dry-run` 是轻量预检：只检查路径、权限、文件大小、配置、Prompt、taxonomy 和 cluster 头部样本；它不会完整扫描 cluster、不会建立索引、不会创建正式 run，并会明确提示完整数据尚未验证。

首次处理 50GB+ cluster 时，建议先单独完成准备：

```powershell
python -m filter_v2.run_pipeline `
  --prepare-only
```

准备阶段只流式解析 cluster 一次，完成严格预检、SHA-256 和 SQLite 定位索引；不调用模型、不发布。之后执行相同正常命令会复用该 run 和索引。

## 输入、工作区与发布边界

`source-root` 只读以下初版文件：

```text
P0-冒烟集/P0_smoke_set_v4_labeled.jsonl
P1-基准集/P1_benchmark_set_v4_labeled.jsonl
P2-对抗集/P2_adversarial_set_v4_labeled.jsonl
```

默认工作区位于当前 Python 包旁，不依赖 `filter_v2` 字面量：

```text
<package>/outputs/<week>__<source_id>/runs/<timestamp>__<config_id>/
├── manifest.json
├── input_audit.json
├── logs/pipeline.log
├── prompts/validity_filter_prompt.md
├── screening_summary.json
├── supplement_report.md
├── P0/
│   ├── prescreen_attempts.jsonl
│   ├── prescreen_results.jsonl
│   ├── P0_smoke_set_v4_prescreen.jsonl
│   ├── P0_smoke_set_v4_screened.jsonl
│   └── screening_summary.json
├── P1/
│   ├── prescreen_attempts.jsonl
│   ├── prescreen_results.jsonl
│   ├── supplement_tasks.jsonl
│   ├── supplement_attempts.jsonl
│   ├── supplement_results.jsonl
│   ├── supplement_summary.json
│   ├── screened_provenance.jsonl
│   ├── P1_benchmark_set_v4_prescreen.jsonl
│   ├── P1_benchmark_set_v4_supplement.jsonl
│   ├── P1_benchmark_set_v4_screened.jsonl
│   └── screening_summary.json
└── P2/
    ├── prescreen_attempts.jsonl
    ├── prescreen_results.jsonl
    ├── P2_adversarial_set_v4_prescreen.jsonl
    ├── P2_adversarial_set_v4_screened.jsonl
    └── screening_summary.json
```

大文件索引位于输出根目录，而不在 cluster 所在目录：

```text
<output_root>/_cluster_cache/<cluster_stat_id>/
├── metadata.json
├── input_audit.json
└── cluster_index.sqlite
```

构建中还会暂存可恢复数据库和排他锁。未完成、`--no-publish` 或发布失败时保留索引；发布成功后默认删除 SQLite 和锁，只保留小型元信息与输入审计。索引契约升级时，仅清理已确认属于该缓存根目录、同一 source 的旧版生成数据库，并保留失效元数据；不会修改原始 cluster、taxonomy 或已发布数据。可用 `--cluster-cache-root` 将其放到容量更大的本地 SSD。

P0/P2 不生成 supplement 空文件。只有所有选中分支完成后，才将各集合 `*_screened.jsonl` 以事务式原子替换发布到目标目录；其他文件始终留在 run 工作区。

`*_prescreen.jsonl` 表示“初筛后按该集合策略接纳”，因此 P2 文件同时包含 KEEP 和可测困难 REVIEW。P1 的 prescreen 与 supplement 文件保存各阶段全部合格样本；最终 screened 再按配额裁剪。

## P1 Population 和增量补样

- L1 Population 以 taxonomy 的 `total_size` 为准；L2 诊断以 `sub_intents.size` 为准。
- taxonomy 的 `pct` 会按 `total_size` 重算并作为诊断展示。
- cluster JSONL 只提供候选池、追溯和统计诊断，不再用去重行数覆盖 taxonomy Population。首次扫描以二进制逐行处理，内存不随总行数增长。
- `cluster=-1` 和未映射 cluster 不进入 Population 配额或候选池。
- `frequency` 仅用于口径诊断，`cluster_prob` 不参与 Population、配额或默认抽样。
- taxonomy `_meta`（包括 `coverage`、`assigned`、`total` 等）原样写入审计，不附加上游未声明的强业务语义，也不据此判断完整覆盖。
- taxonomy 与 cluster 会计算完整双向差集：cluster-only 告警并排除；taxonomy-only 告警但保留其 L1 配额，候选不足继续走现有短缺逻辑。
- L2 `size` 总和与 L1 `total_size` 的差异只记录为诊断；taxonomy 必需结构、唯一归属和父子映射仍严格校验。
- 初版记录有 cluster 时以 taxonomy 映射为准；原 L1 与映射冲突会失败。没有 cluster 时使用与 taxonomy 精确匹配的原 L1。
- L1 权重为 `Population ^ alpha`，默认 `alpha=0.5`；最大余数法确保整数配额总和严格等于 `target_n`。
- `target_n` 默认等于 P1 初版全部合法 JSONL 记录数。

所有 labeled source 记录必须提供 1～5 的整数 `quality_overall_score`，按现有回退规则得到的目标 Query 以及去重身份的 session、文本部分都必须非空。Cluster ID 只能统一使用整数或统一使用非空字符串（噪音值 `-1`/`"-1"` 除外），不自动进行数字/字符串转换。非噪音 Cluster 记录同样要求 Query 和去重身份非空；`-1`/`"-1"` 会直接按噪音排除，不要求这些候选字段。相同去重身份若对应不同目标 Query 或 cluster 会在任何模型调用前失败；完全一致的重复记录仍确定性去重。错误与日志只包含文件、物理行、问题类型和有限样例，不写原始 Query 内容。

完整统计写入缓存和 run 根目录的 `input_audit.json`，manifest 保存审计哈希、摘要和明确采用的 Population、`target_n`、去重口径。源输入在 run 创建前失败时，精简审计写入 `<output_root>/_input_audits/`，异常会给出具体路径。

第一轮按初筛结果估算每个 L1 的通过率：

```text
全局率 = P1 初筛 KEEP / P1 初筛合法终态
分类率 = (分类 KEEP + prior_strength × 全局率) / (分类已筛 + prior_strength)
```

后续轮次以上一阶段分类率为先验，使用该 L1 已完成的上游补样结果继续更新。默认先验强度为 10，通过率限制在 `[0.10, 0.95]`，无观察量时回退 0.55。每轮请求量为：

```text
ceil(缺口 / 分类估计通过率 × safety_factor)
```

默认安全系数 1.10、最多 3 轮。候选耗尽或达到轮次上限后允许带业务缺口成功结束；不会降标、启用旧低分样本或向其他类别转移配额。

## 统一质检协议

初版和 P1 增量候选使用同一 Prompt 快照。cluster 记录以 `raw_query` 为目标轮、`text` 为多轮上下文。响应必须符合：

```json
{
  "decision": "KEEP",
  "review_class": null,
  "checks": {
    "has_user_goal": true,
    "goal_identifiable": true,
    "context_sufficient": true,
    "testable": true
  },
  "interpreted_goal": "用户目标",
  "reason_codes": ["VALID_ACTION"],
  "judge_confidence": 0.93,
  "short_reason": "简短说明"
}
```

REVIEW 必须填写 `TESTABLE_DIFFICULTY` 或 `UNUSABLE`；非 REVIEW 必须为 `null`。原因码必须与决定和 REVIEW 分类匹配。

无法解析或字段不符合协议属于工程失败，不进入规范 results。`quality_control.max_retries` 表示包含首次调用在内的最大尝试次数：第一波处理当前阶段全部待处理任务，后续波次只调用上一波仍失败的任务，并在每波结束后原子保存新增合法 results。三波用尽仍失败时保持 incomplete 且不发布；再次运行相同命令仍只调用缺少合法终态的任务。重试波次只处理工程失败，不等同于 P1 根据配额重新抽取候选的补样轮次。

## 并行、恢复与发布

- P0/P1/P2 分支并行，但 `quality_control.workers` 是全链路最大在途模型请求数，不会按分支倍增。
- cluster 完整索引和映射预检必须在任何模型调用前完成；完成后，P1 的小型准备工作可与三个分支初筛并行。
- 单分支工程失败不会丢弃其他分支进度，但任何未完成分支都会阻止总体报告和发布。
- 鉴权、非法请求等全局配置错误会停止所有分支。
- 普通调用或协议错误按分支执行失败集合逐波收缩；波次之间使用 2 秒、4 秒的退避，已有合法结果不会重复调用。
- 补样任务在调用前写入不可变任务账本；恢复时重试同一轮缺少终态的任务。
- 补样轮次状态保存在 manifest，不再生成独立 state 文件。
- 相同源文件、外部输入、Prompt 和语义配置优先恢复未完成 run；成功 run 默认复用且不重复调用模型。
- `--new-run` 强制创建新运行；`--run-id` 显式恢复兼容运行。
- 发布前默认验证 cluster 的路径、大小、修改时间、文件标识和抽样指纹，并依据 `screened_provenance.jsonl` 定点回读最终上游样本；不会再次扫描 500 万行。`--verify-full-cluster-hash` 可显式要求发布前重新计算完整 SHA-256。

退出码：`0` 成功，`1` 配置、数据或发布错误，`2` 存在可续跑的模型工程失败。

## 运行时延

时延不需要额外配置。`manifest.json` 的顶层 `timing` 是唯一结构化账本，累计记录首次开始、完成时间、自然时间跨度、活跃执行耗时、启动/恢复次数，以及初始化、Cluster 准备、Quality Control、汇总、报告、发布和各集合分支的执行次数与耗时。阶段失败也会保留已经执行的耗时；断点恢复期间程序未运行的时间只进入自然时间跨度，不进入活跃耗时。

最终 `supplement_report.md` 展示上述汇总，并直接从现有 attempts 计算各 prescreen 和 P1 补样阶段的首 Token、完整响应时延，包括有时延记录的调用数、平均值、P50、P95 和最大值。重试按独立 attempt 计数，没有时延的异常调用不进入统计，分位数使用 nearest-rank 口径。并行分支耗时和 LLM 调用耗时可能重叠，不能相加作为整链路耗时；整链路性能以顶层活跃执行耗时为准。

报告会在发布及索引清理结束后刷新一次，使发布耗时和最终总耗时进入报告并同步更新 manifest 中的报告哈希。最后这次毫秒级文件刷新本身不计入总耗时。

## 配置与诊断

链路配置位于 `artifacts/config/pipeline.default.json`，其中 `inputs.source_root`、`inputs.taxonomy` 和 `inputs.upstream_cluster_population` 对应同一个 week。优先级为 `CLI > --config 指定 JSON > 包内默认配置`，三个输入路径仍可通过 CLI 临时覆盖。LLM 连接、模型、`max_tokens`、超时和流式选项统一位于被忽略的 `artifacts/config/llm_config.local.json`，不接受 CLI 或环境变量覆盖。

常用 P1 覆盖参数包括：

- `--p1-supplement/--no-p1-supplement`
- `--p1-alpha`
- `--p1-target-n`
- `--p1-max-rounds`
- `--p1-default-pass-rate`
- `--p1-pass-rate-prior-strength`
- `--p1-min-pass-rate/--p1-max-pass-rate`
- `--p1-safety-factor`
- `--cluster-cache-root`
- `--cluster-index-batch-size/--cluster-progress-every`
- `--cluster-min-free-space-gb/--cluster-reserve-free-space-gb`
- `--verify-full-cluster-hash`

P1 阶段和 taxonomy 诊断工具仍可独立运行，但必须显式提供 source/run 或输出位置，不会向上游数据目录写中间产物。
