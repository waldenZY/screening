# Screening v3

`screening` v3 从同一份 cluster JSONL 和 taxonomy 构建 P0/P1/P2。它不读取旧 labeled
数据作为候选来源，也不兼容或恢复旧版 run manifest。

Screening v2 已冻结在 Git `v2` 分支及提交 `b894753`；对应配置、Prompt 和文档快照见
[V2 归档](artifacts/archive/v2/README.md)。V3 不会恢复或发布 `pipeline_version=2` 的 run。

## 处理链路

```text
cluster + taxonomy
→ canonical 流式索引与固定特征
→ P0/P1 独立采样；P2 四来源自适应候选召回
→ 第一阶段：有效性、基础质量及类别检测
→ 仅 P1 按冻结 L2 配额补采（每轮分小批，最多三轮）
→ P2 Challenge Judge、类型配额选择与最多三轮补采
→ 第二阶段：难度及综合质量
→ canonical screened
→ 28 字段 legacy labeled 导出
→ JSON/Markdown 批跑报告
→ 校验与发布
```

- P0 沿用结构采样，无全局数量目标，不补采。
- P1 第一阶段通过后的目标为 10,000，并按冻结 L2 配额独立补采。
- P1 补采保留每轮请求数公式；每批最多抽当前 L2 缺口数，批后根据 cluster
  Stage1 通过情况重新分配下一批。配额填满或候选耗尽的 L2 立即停止。
- P2 目标为 2,000 条 Stage1 PASS 且 Challenge PASS 的样本；八类 primary 各有软下限 100、单类上限 700，并支持最多三轮自适应补采。
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
run 会直接复用。恢复以阶段检查点为事实源：已完成的 P0/P1 采样、Stage1、P1
补采小批、P2 自适应小批、最终成员和 Stage2 都不会重新执行。输入、Prompt、模型或业务配置变化时
会建立新 run。

Stage1 是三个集合统一的基础质量准入；P2 还必须通过 Challenge Judge。Stage2 可以
给出与 Stage1 不同的独立质量判断；这种情况会
作为“跨阶段分歧”写入 screened canonical、脱敏一致性账本和批跑报告，不会重试、
中断或改变集合成员。非法 JSON、字段/枚举错误、Challenge 协议矛盾和 Stage2 内部
评分矛盾属于可续跑的工程失败。

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
所在目录为基准。CLI 显式参数优先于配置值。三个 Prompt 默认位于
`artifacts/prompts/quality_stage1.md`、`quality_challenge.md` 和 `quality_stage2.md`。常用临时覆盖参数：

每个配置字段的含义、范围、调大/调小影响和 run identity 关系，参见
[配置使用手册](artifacts/docs/screening_pipeline_configuration_v3.md)。P1 分批补采由 V2 引入，
历史设计见 [归档中的 P1 补采优化说明](artifacts/archive/v2/docs/screening_v2_p1_adaptive_supplement.md)；
当前 P2 细节见 [P2 Adaptive 技术方案](artifacts/docs/screening_v3_p2_adaptive.md)。
尚未实施的 P0 后续设想单独记录在
[P0 Adaptive 讨论稿](artifacts/docs/screening_v3_p0_adaptive_proposal.md)，不能作为当前运行口径。

- `--source`、`--taxonomy` 和 `--output-root`；
- `--workers`、`--max-retries` 和 `--sleep`；
- `--run-id`，只恢复契约完全一致的 v3 run；
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
│   ├── canonical_candidate_index.sqlite
│   ├── canonical_metadata.json
│   ├── canonical_input_audit.json
│   └── canonical_database_validation.json
└── runs/<run-id>/
    ├── manifest.json
    ├── checkpoints/
    ├── p012_build_summary.json
    ├── legacy_export_report.json
    ├── screening_run_report.json
    ├── screening_run_report.md
    ├── quality/
    │   ├── stage1_tasks.jsonl
    │   ├── stage1_attempts.jsonl
    │   ├── stage1_cache_hits.jsonl
    │   ├── stage1_results.jsonl
    │   ├── challenge_tasks.jsonl
    │   ├── challenge_attempts.jsonl
    │   ├── challenge_cache_hits.jsonl
    │   ├── challenge_results.jsonl
    │   ├── stage2_tasks.jsonl
    │   ├── stage2_attempts.jsonl
    │   ├── stage2_cache_hits.jsonl
    │   ├── stage2_results.jsonl
    │   ├── stage2_consistency.jsonl
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

Run 根目录的两份批跑报告来自同一份指标事实源：

- `screening_run_report.json`：机器可读指标，适合看板、版本对比和二次分析；
- `screening_run_report.md`：JSON 指标的可读摘要，包含 P0/P1/P2 核心指标、L1/L2
  语义树分布、P1 配额完成率、P2 来源及 Challenge 类型漏斗、波次统计和最终难度/质量分布。

报告同时给出 Stage1/Stage2 跨阶段分歧数量、分歧率和分歧类型分布。该指标用于观察
两次独立判断的稳定性，不是新的淘汰门槛。

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

面向下游或对外沟通的完整字段字典见
[Screening 最终交付数据字段说明（labeled v4）](artifacts/docs/screening_labeled_v4_field_dictionary.md)。

P0/P1 第 15 个字段为 `relevance_p0`，P2 为 `relevance_p2`；其余字段名、类型和
顺序一致。`norm_freq` 与 relevance 在导出时保留四位小数。所有字段必须存在且
不得为 `null`，只有 `context_text` 可以是空字符串。

多轮记录中，`context_text` 以 ` | ` 连接前序轮次，`target_query` 为最后一轮，
`text` 为两者组合；单轮记录的 `text=target_query` 且 `context_text=""`。

发布前会独立重验 canonical、Stage1/Challenge/Stage2 标签、第一阶段准入、任务账本、P1 冻结配额、
逐 L2 缺口、批跑报告指标、legacy 映射、provenance 和 manifest 哈希。任何必需
LLM 任务失败、报告被修改或字段契约不一致都会阻止发布。
