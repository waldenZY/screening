# 数据筛选链路实现技术方案 v1

> 状态：当前实现方案。
>
> 版本边界：v1 指“P0/P1/P2 独立策略、P1 Coverage 固定配额、统一质检、上游新候选补样、50GB+ Cluster 索引化、隔离工作区和原子发布”方案。

## 1. 建设目标

v1 将三个集合视为目标不同、可独立运行的分支：

- P0：稳定、可用、适合快速回归，只接纳初筛 KEEP；
- P1：面向意图覆盖，按上游 Population 建立固定 L1 配额，质量合格后再按配额选择；
- P2：面向复杂、边界和困难场景诊断，接纳 KEEP 及真实可测困难 REVIEW。

公共能力包括统一质检、全局模型并发控制、调用轨迹、断点恢复、运行清单、日志、最终校验和原子发布。

## 2. 输入、目录与写入边界

### 2.1 输入

一次完整 P0/P1/P2 运行需要：

1. 周数据目录：包含三个初版 labeled 文件；
2. taxonomy：两层 L1/L2 意图分类树；
3. Cluster 全集：JSONL，每行一条上游真实 Query 记录；
4. 统一质检 Prompt；
5. JSON 配置和必要的模型环境变量。

仅运行 P0/P2 时不读取 taxonomy 和 Cluster。

### 2.2 三类目录

```text
source_root   原始周数据；运行中只读
run_root      配置、日志、轨迹、中间结果和权威最终副本
publish_root  发布目标；默认等于 source_root
```

源目录只允许在整条链路成功后写入选中集合的 `*_screened.jsonl`。任何中间结果和报告都不得写入源目录。Cluster 原文件始终只读；索引缓存默认位于 `output_root`，不会写到 Cluster 文件旁。若显式覆盖缓存根目录，则写入边界由该参数决定。

### 2.3 Run 身份

Run 兼容性由以下内容共同决定：

- 周数据规范路径及内容哈希；
- taxonomy 路径及 SHA-256；
- Cluster 路径、大小、修改时间、文件标识及首次完整 SHA-256；
- Prompt SHA-256；
- 影响筛选语义的配置；
- 管线语义版本和索引版本。

输入或语义配置发生变化时创建新 run，不混用旧结果。相同成功 run 默认复用，不重复消费模型额度。

## 3. 总体流程

```text
轻量预检（可选 dry-run）
        │
Cluster 单次流式严格预检与 SQLite 索引
        │
        ├──────────┬──────────┐
        │          │          │
       P0         P1         P2
     统一初筛    统一初筛    统一初筛
        │          │          │
    接纳 KEEP   KEEP 合格池  KEEP + 可测困难 REVIEW
        │          │          │
        │       L1 配额/缺口   │
        │          │          │
        │       最多三轮补样   │
        │          │          │
        └──────最终构建与校验──┘
                   │
             总体摘要和报告
                   │
             全集合原子发布
                   │
          成功后清理大型 SQLite
```

Cluster 严格预检必须在任何模型调用前完成。之后三个分支并行运行，但共享一个全局模型并发上限。

## 4. 七环节实现逻辑

### 4.1 目标分布

只有 P1 建立目标分布。

#### Population 口径

- L1 Population 使用 taxonomy 的 `total_size`；
- L2 Population 使用 taxonomy 的 `sub_intents.size`；
- taxonomy 的 `pct` 重新计算后只用于展示和一致性诊断；
- Cluster 行数不覆盖 taxonomy Population；
- `frequency` 只进入诊断，不参与配额；
- `cluster_prob` 不参与 Population、配额或默认抽样；
- 噪音簇和 taxonomy 未映射 Cluster 不进入配额或候选池；
- taxonomy-only Cluster 保留所在 L1 的 Population 与配额，候选不足按既有短缺逻辑处理；
- taxonomy `_meta` 仅原样审计，不从 `coverage`、`assigned`、`total` 等字段推断 Cluster 完整覆盖；
- L2 `size` 与父 L1 `total_size` 的差异仅作诊断，因为没有上游文档证明两者同口径。

#### L1 权重与整数配额

```text
weight[L1] = Population[L1] ^ alpha
share[L1]  = weight[L1] / Σweight
```

默认 `alpha=0.5`。`target_n` 默认等于 P1 初版全部合法记录数，也可显式配置。

整数配额通过最大余数法生成：

- 配额总和严格等于 `target_n`；
- Population 为 0 的 L1 配额为 0；
- 相同输入和配置下结果完全稳定。

P0、P2 不计算目标比例，也不要求保持初版类别分布。

### 4.2 候选池

#### 初版候选

| 集合 | 进入统一初筛的质量分 |
|---|---|
| P0 | 2～5 分 |
| P1 | 3～5 分 |
| P2 | 3～5 分 |

范围外样本不调用模型、不进入正式集，但进入数量统计。

#### P1 增量候选

来自 taxonomy 映射成功的上游 Cluster 全集，并排除：

- `cluster=-1`；
- taxonomy 未映射 Cluster；
- P0/P1/P2 初版中已经使用的样本；
- 本 run 已经冻结或质检过的补样样本；
- 按 `session/session_id + text` 判断的重复样本。

上游候选不要求质量分。`raw_query` 作为目标 Query，`text` 作为上下文；如果 `text` 最后一轮与 `raw_query` 相同，则从上下文中去除重复目标轮。

### 4.3 初筛

三个集合和 P1 增量候选使用同一份 Prompt 快照、同一模型标准和同一结构化协议：

```json
{
  "decision": "KEEP | REVIEW | REJECT",
  "review_class": "TESTABLE_DIFFICULTY | UNUSABLE | null",
  "checks": {
    "has_user_goal": true,
    "goal_identifiable": true,
    "context_sufficient": true,
    "testable": true
  },
  "interpreted_goal": "...",
  "reason_codes": ["..."],
  "judge_confidence": 0.9,
  "short_reason": "..."
}
```

协议约束：

- REVIEW 必须提供合法 `review_class`；
- 非 REVIEW 的 `review_class` 必须为 `null`；
- `checks`、原因码和决定必须相互一致；
- 无法解析、缺字段或字段冲突均属于工程失败，不生成规范终态。

### 4.4 分布变化与初筛接纳

#### P0

- 接纳全部 KEEP；
- REVIEW、REJECT 全部排除；
- 不补样、不裁剪；
- 初筛接纳集与最终 screened 相同。

#### P1

- KEEP 进入 Qualified Pool，但不自动正式入选；
- REVIEW、REJECT 不进入合格池；
- 按 L1 统计合格数量与固定配额的差值；
- 超额 L1 的 KEEP 保留在审计和阶段产物中，最终只选择配额数量；
- 不足 L1 形成明确缺口。

```text
deficit[L1] = max(0, quota[L1] - qualified[L1])
```

#### P2

- 接纳全部 KEEP；
- 接纳 `TESTABLE_DIFFICULTY` REVIEW；
- 排除 `UNUSABLE` REVIEW 和 REJECT；
- 不做配额裁剪和默认补样。

### 4.5 P1 补样

只有 P1 默认启用补样，按 L1 缺口从对应 L1 的索引候选中继续取样。

#### 自适应通过率

初筛得到全局通过率：

```text
global_rate = P1 初筛 KEEP / P1 初筛合法终态
```

无观察量时回退默认值 0.55。第一轮每个 L1 使用平滑估计：

```text
rate[L1]
= (L1 初筛 KEEP + prior_strength × global_rate)
  / (L1 初筛终态 + prior_strength)
```

默认先验强度为 10，结果限制在 `[0.10, 0.95]`。

后续轮次以上一阶段的 L1 估计为先验，使用该 L1 已完成的上游补样 KEEP/终态继续更新。

#### 每轮抽取量

```text
request[L1]
= ceil(deficit[L1] / estimated_rate[L1] × safety_factor)
```

默认安全系数 1.10、最多 3 轮。通过率只影响请求数量，不改变配额和质检标准。

每轮任务在模型调用前冻结。若本轮存在工程失败，则完成已调度请求后停止，不规划下一轮；恢复时只重试本轮缺少合法终态的任务。

#### 未补齐处理

- 达到配额：`quota_filled`；
- 候选提前耗尽：`candidate_exhausted`；
- 三轮结束仍有缺口：`max_rounds`。

后两种属于允许发布的业务短缺：不降标、不启用低分旧样本、不跨 L1 转移配额。工程失败则不同，它会使分支不完整并阻止发布。

### 4.6 复筛语义

v1 不再存在“对已有 REJECT 降低标准重新审核”的旧式复筛。

P1 每轮上游候选都是首次接受统一 QC；其工程位置相当于补样审核，但质量标准与初版完全一致。P0、P2 当前没有补样阶段，配置结构仅为未来扩展保留接口。

### 4.7 最终选择

#### P0

全部初筛 KEEP，按原始行序输出。

#### P1

初版 KEEP 和上游补样 KEEP 共同组成 Qualified Pool。每个 L1 使用固定 seed 和稳定身份排序，最多选择到该 L1 配额：

```text
QUALIFIED + quota
→ SELECTED
→ QUALIFIED_NOT_SELECTED
```

- 初版入选记录保持原始字段；
- 上游入选记录保持上游原字段，并增加目标类别字段；
- 最终文件允许同时存在两种原生记录结构；
- 先输出初版来源，再输出上游来源，各来源内按物理行号升序；
- 全局按样本身份去重。

#### P2

全部 KEEP 与可测困难 REVIEW 的并集，保持初版结构和原始行序。质检原因只留在工作区审计结果中。

## 5. 50GB+ Cluster 处理

### 5.1 单次流式索引

首次准备以二进制逐行扫描 Cluster，同时完成：

- 完整 SHA-256；
- UTF-8、空行、CRLF 和 JSONL 校验；
- 物理行号、字节偏移和字节长度记录；
- taxonomy 映射、噪音和未映射统计；
- taxonomy/Cluster 双向 ID 差集与 ID 类型分布；
- Cluster ID、目标 Query、去重身份和身份冲突校验；
- 噪音值 `-1`/`"-1"` 直接排除，不要求 Query 或去重身份字段；
- frequency、行数和 taxonomy 总量诊断；
- 身份去重；
- SQLite 候选定位索引写入。

SQLite 不复制完整 JSON，只保存：

- 身份哈希；
- 原始行号、字节偏移、字节长度；
- Cluster、L1、L2；
- 稳定随机排名；
- 原始记录指纹。

索引建立 `(L1, 稳定排名, 原始行号)` 查询顺序，内存占用只与单行、批次和本轮抽样规模有关。

### 5.2 中断恢复与互斥

- 每固定批次提交 SQLite 事务和扫描检查点；
- 恢复时从文件开头快速重算检查点之前的 SHA-256，但不重复 JSON 解析和数据库插入；
- 从检查点字节偏移继续解析；
- 文件属性变化、检查点非法或数据库损坏时拒绝混用；
- 文件级 I/O 锁防止同一 Cluster 被多个进程同时全量扫描；
- 锁通过 PID 和进程启动标识识别过期状态。

### 5.3 资源控制

- 建索引前默认要求至少 3GB 可用空间；
- 运行中默认保留至少 1GB 安全余量；
- 每 10 万条记录输出行数、字节数、吞吐量、耗时、预计剩余时间、索引大小和磁盘余量；
- taxonomy、seed 或索引语义版本变化时建立兼容的新索引。
- 同一 source 的旧索引版本只删除缓存根目录内的生成数据库，保留失效元数据，避免同时占用两份大索引空间。

### 5.4 定点读取与清理

- 补样只查询当前 L1 所需的前 K 个未使用定位信息；
- 模型输入和最终构建只按字节偏移读取被选中的记录；
- 发布校验只回读最终入选的上游记录；
- 默认不重新扫描完整 Cluster；
- 显式严格哈希模式会额外重算一次完整 SHA-256；
- 成功发布后删除大型 SQLite，保留小型元信息；
- 未完成、禁止发布或发布失败时保留索引供恢复。

## 6. 并行、失败与断点续跑

### 6.1 并行模型

P0、P1、P2 分支并行，但共享一个全局信号量。配置的 worker 数表示整条链路最大在途模型请求数，而不是每个分支各自拥有同样数量。

### 6.2 调用轨迹与规范结果

- Attempts 追加保存每次请求、响应、耗时、错误类别和重试序号，并及时刷新；
- Results 原子重建，只保存每个任务当前唯一合法终态；
- 已有合法终态不会在恢复时重复调用；
- 解析失败、网络失败、超时、限流和服务端错误只进入调用轨迹；
- 鉴权或非法请求等全局配置错误停止全部分支。

### 6.3 分支与发布门禁

- 单分支失败不删除其他分支已完成进度；
- 任一选中分支存在工程失败，整条链路不发布；
- P1 业务缺口不阻止发布；
- 发布失败后可直接使用 run 内权威 screened 重试，不重新调用模型。

## 7. 工作区产物

```text
run_root/
├── manifest.json
├── input_audit.json
├── logs/pipeline.log
├── prompts/validity_filter_prompt.md
├── screening_summary.json
├── supplement_report.md
├── P0/
│   ├── prescreen_attempts.jsonl
│   ├── prescreen_results.jsonl
│   ├── *_prescreen.jsonl
│   ├── *_screened.jsonl
│   └── screening_summary.json
├── P1/
│   ├── prescreen_attempts.jsonl
│   ├── prescreen_results.jsonl
│   ├── supplement_tasks.jsonl
│   ├── supplement_attempts.jsonl
│   ├── supplement_results.jsonl
│   ├── supplement_summary.json
│   ├── screened_provenance.jsonl
│   ├── *_prescreen.jsonl
│   ├── *_supplement.jsonl
│   ├── *_screened.jsonl
│   └── screening_summary.json
└── P2/
    ├── prescreen_attempts.jsonl
    ├── prescreen_results.jsonl
    ├── *_prescreen.jsonl
    ├── *_screened.jsonl
    └── screening_summary.json
```

P0/P2 不生成 supplement 空文件。P1 的 supplement 数据集保存全部补样 KEEP，不等同于最终按配额入选的子集。

## 8. 来源账本与发布校验

P1 最终构建同步生成来源账本，每条最终样本记录：

- 输出位置和来源类型；
- 初版或上游物理行号；
- 补样任务 ID 和轮次；
- 字节偏移与长度；
- Cluster、L1、L2；
- 身份哈希；
- 原始记录和输出记录指纹。

发布前验证：

1. 周数据和 taxonomy 内容未变化；
2. Cluster 路径、大小、修改时间、文件标识及抽样指纹未变化；
3. 全部规范结果合法、唯一且任务覆盖完整；
4. 来源账本与任务账本一致；
5. 定点回读记录、目标字段、指纹、去重和顺序正确；
6. 三个正式文件先写临时文件，再作为一个事务提交。

只有事务完整成功，源目录才看到新的 screened 文件。

## 9. 报告体系

| 产物 | 职责 |
|---|---|
| `manifest.json` | 整条运行的输入、哈希、配置、阶段状态、产物和发布状态 |
| `input_audit.json` | 原始 taxonomy `_meta`、实际统计、双向差集、字段质量与固定业务口径 |
| 总体 screening summary | P0/P1/P2 的输入、判断和最终数量汇总 |
| P1 supplement summary | Population、配额、通过率、逐轮补样、最终缺口和停止原因 |
| Markdown 报告 | 面向人工核查的集合结果、P1 L1 配额/缺口、轮次、L2 与 Cluster 诊断 |
| pipeline log | 运行过程、并发分支、索引进度、错误和恢复信息 |

报告只保存在 run 工作区，不发布到周数据目录。

## 10. 运行模式

### 轻量预检

```text
dry-run
```

只检查路径、权限、文件大小、可用空间、配置、Prompt、taxonomy 和 Cluster 头部样本；不完整扫描、不建索引、不创建正式 run。

### 完整准备

```text
prepare-only
```

完整扫描 Cluster、建立索引、完成严格预检并创建可恢复 run；不调用模型、不发布。

### 正常运行

自动恢复相同源和配置的最近未完成 run；成功 run 校验后直接复用。强制重审时创建新 run。

### 严格 Cluster 校验

默认按照“Cluster 生成后不可变”处理，恢复和发布使用文件属性及抽样指纹快速校验。必要时启用完整哈希校验，代价是额外顺序读取整个文件一次。

## 11. 当前限制与后续方向

1. P1 目前只有 L1 硬配额；L2 只做覆盖诊断。
2. L1 内部由稳定候选顺序自然选择 L2，不能保证 L2 比例或最低覆盖。
3. 更合理的下一步是“L1 硬配额 + L2 软目标”，而不是直接建立 2,595 个全局 L2 硬配额。
4. P0/P2 已预留补样策略接口，但当前只有 P1 实现 Coverage 补样。
5. P1 正式文件允许初版记录与上游记录两种原生结构，消费方不能假设所有字段完全一致。
6. 首次 50GB+ 索引耗时不可消除；上线前应通过真实数据的准备模式测量吞吐、索引大小和磁盘峰值。

历史方案请参见 [数据筛选链路实现技术方案 v0](screening_pipeline_technical_design_v0.md)。
