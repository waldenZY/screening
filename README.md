# Screening v2

`screening` v2 从同一份 cluster JSONL 和 taxonomy 构建 P0/P1/P2。它不读取旧 labeled
数据作为候选来源，也不兼容或恢复旧版 run manifest。

## 处理链路

```text
cluster + taxonomy
→ canonical 流式索引与固定特征
→ P0/P1/P2 独立采样和集合内去重
→ 第一阶段：有效性、基础质量及类别检测
→ 仅 P1 按冻结 L2 配额补采（最多三轮）
→ 第二阶段：难度及综合质量
→ canonical screened
→ 28 字段 legacy labeled 导出
→ JSON/Markdown 批跑报告
→ 校验与发布
```

- P0 沿用结构采样，无全局数量目标，不补采。
- P1 第一阶段通过后的目标为 10,000，只有 P1 补采。
- P2 初始采样目标为 3,000，不补采；第一阶段淘汰后允许不足。
- 三个集合均以 `exact + self_contained + clear + valid` 准入。
- 第二阶段只补充标签，不改变集合成员。
- 不做跨集合去重；完全相同的 LLM 任务在同一 run 内复用。

## 运行

Python 操作使用 `moto` 环境。先复制并填写本地 LLM 配置：

```powershell
Copy-Item screening/artifacts/config/llm_config.example.json `
  screening/artifacts/config/llm_config.local.json
```

然后在 `artifacts/config/pipeline.default.json` 中填写 `inputs.source` 和
`inputs.taxonomy`。默认 `publication.enabled=false`，因此一键启动只构建、不交付：

```powershell
python -m screening.run_pipeline
```

相同配置再次执行会自动恢复未完成 run，只重试缺少合法结果的 LLM 任务；已经完成的
run 会直接复用。输入、Prompt、模型或业务配置变化时会建立新 run。

需要自动交付时，在配置中设置 `publication.enabled=true` 并填写 `publish_root`；也可
使用 CLI 临时覆盖配置：

```powershell
python -m screening.run_pipeline `
  --source D:/data/cluster.jsonl `
  --taxonomy D:/data/taxonomy.json `
  --output-root D:/data/screening-runs `
  --publish-root D:/data/delivery
```

发布目录已有不同内容时默认拒绝。确认整组三个文件都要替换时显式增加
`--replace`。相同内容会直接视为幂等成功。

完整运行配置位于 `artifacts/config/pipeline.default.json`，相对路径以该配置文件
所在目录为基准。CLI 显式参数优先于配置值。两个 Prompt 默认位于
`artifacts/prompts/quality_stage1.md` 和 `quality_stage2.md`。常用临时覆盖参数：

- `--source`、`--taxonomy` 和 `--output-root`；
- `--workers`、`--max-retries` 和 `--sleep`；
- `--run-id`，只恢复契约完全一致的 v2 run；
- `--new-run`，忽略可恢复或可复用 run，新建一次运行；
- `--cache-root`，覆盖默认的 `<output-root>/_canonical_cache`；
- `--verify-full-source-hash`，索引打开时重新验证完整 cluster SHA-256；
- `--no-publish`，临时关闭配置中的自动发布。

`--new-run`、`--run-id` 和 `--replace` 只允许通过 CLI 指定，不写入长期配置。

退出码：`0` 成功，`1` 配置、数据、契约或发布错误，`2` 存在可以续跑的 LLM
工程失败。

## 运行产物与下游交付

工作区结构：

```text
<output-root>/
├── _canonical_cache/<cache-id>/
└── runs/<run-id>/
    ├── manifest.json
    ├── p012_build_summary.json
    ├── legacy_export_report.json
    ├── screening_run_report.json
    ├── screening_run_report.md
    ├── quality/
    │   ├── stage1_tasks.jsonl
    │   ├── stage1_results.jsonl
    │   ├── stage2_tasks.jsonl
    │   ├── stage2_results.jsonl
    │   └── llm_result_cache.sqlite
    ├── P0/
    ├── P1/
    └── P2/
```

每个分支保存：

- `screened.canonical.jsonl`：内部权威成品；
- `*_labeled.jsonl`：旧版 28 字段兼容视图；
- `export_provenance.jsonl`：source/canonical/legacy 逐条哈希关联；
- `legacy_export_report.json`：字段、类型、空值、枚举与数值转换检查；
- `candidate_tasks.jsonl` 和 `branch_audit.json`：候选来源及分支统计。

Run 根目录的两份批跑报告内容等价：

- `screening_run_report.json`：机器可读指标，适合看板、版本对比和二次分析；
- `screening_run_report.md`：可直接用于汇报，包含 P0/P1/P2 核心指标、L1/L2
  语义树分布、P1 配额完成率、波次统计和最终难度/质量分布。

报告中的 Query 基础可用率使用 `self_contained + clear + valid`；语义标签一致率是在
这些可判断 Query 中统计 `category_fit=exact` 的比例。该一致率是 LLM-as-judge
自动评估结果，不等同于人工金标准确率。P1 的全面性主要通过冻结配额 L2 覆盖率、
配额完成率和逐 L2 缺口说明。

下游发布目录只接收：

```text
P0-冒烟集/P0_smoke_set_v4_labeled.jsonl
P1-基准集/P1_benchmark_set_v4_labeled.jsonl
P2-对抗集/P2_adversarial_set_v4_labeled.jsonl
```

三份文件作为一个事务提交；任一提交失败会恢复发布前的全部文件。

## Legacy 28 字段契约

P0/P1 第 15 个字段为 `relevance_p0`，P2 为 `relevance_p2`；其余字段名、类型和
顺序一致。`norm_freq` 与 relevance 在导出时保留四位小数。所有字段必须存在且
不得为 `null`，只有 `context_text` 可以是空字符串。

多轮记录中，`context_text` 以 ` | ` 连接前序轮次，`target_query` 为最后一轮，
`text` 为两者组合；单轮记录的 `text=target_query` 且 `context_text=""`。

发布前会独立重验 canonical、两阶段标签、第一阶段准入、任务账本、P1 冻结配额、
逐 L2 缺口、批跑报告指标、legacy 映射、provenance 和 manifest 哈希。任何必需
LLM 任务失败、报告被修改或字段契约不一致都会阻止发布。
