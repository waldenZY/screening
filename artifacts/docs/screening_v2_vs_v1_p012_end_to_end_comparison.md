# Screening v2 与“Screening v1 + P012build”端到端对比报告

> 文档用途：技术评审、方案汇报与版本迁移说明  
> 对比范围：从 cluster 与 taxonomy 输入，到 P0/P1/P2 最终交付数据集  
> 当前版本：Screening v2  
> 更新日期：2026-09-12

## 1. 执行摘要

Screening v2 不是对旧筛选脚本的局部升级，而是把原先分散在 **P012build** 和
**Screening v1** 中的构建、质量判断、补采、字段对齐和发布能力，重新整理成一条
从 cluster 出发的闭环生产链路。

旧方案实际上由两个独立系统串联：

1. P012build 从 cluster 构建 P0/P1/P2，计算固定字段，并通过两轮 LLM 生成
   `quality_*` 标签；
2. Screening v1 再读取这三份 labeled 数据，根据旧质量分区间预选候选，额外调用
   一次有效性模型；P1 不足时才重新从 cluster 中补样。

这种串联方式的核心问题是：**初始样本和补采样本没有经过同一套完整处理**。初始
样本有 P012build 的固定字段和八项质量标签，补采样本只经过 Screening v1 的有效性
审核，无法可靠补齐的字段最终只能写成 `null`。同时，采样、准入和质量分级的职责
分散在两套程序中，规则重复且语义容易漂移。

v2 将链路统一为：

```text
cluster + taxonomy
→ 严格校验与 canonical 索引
→ P0/P1/P2 独立采样
→ 第一阶段：有效性、基础质量及类别检测
→ 第一阶段通过即准入
→ 仅 P1 按 L2 缺口补采
→ 第二阶段：难度及综合质量
→ screened canonical
→ legacy 28 字段映射与复验
→ 三文件事务发布
```

从业务视角看，v2 完成了四项关键转变：

- 从“已有 labeled 数据为主、cluster 只用于补采”转为“cluster 是全部集合的唯一
  样本来源”；
- 从“先打质量分、再按分数预选、再做有效性审核”转为“先统一判断能否准入，再对
  最终成员补充难度与综合质量标签”；
- 从“补采记录被动投影到旧 Schema”转为“所有记录先形成完整 canonical，再统一
  导出 28 字段”；
- 从“两套程序、多个入口和多种账本”转为“一个 run contract、一个生产入口和一套
  发布门禁”。

## 2. 对比基线定义

### 2.1 旧基线：“Screening v1 + P012build”

本报告中的旧基线不是单独的 Screening v1，而是实际需要串联运行的完整体系：

```text
cluster + taxonomy
→ P012build 全量加载、固定字段计算与采样
→ P0/P1/P2 未标注文件
→ P012build 历史 Pass 1：D1～D4
→ P012build 历史 Pass 2：D5～D8
→ P0/P1/P2 labeled 文件
→ Screening v1 按已有 quality_overall_score 区间预选
→ Screening v1 统一有效性审核：KEEP / REVIEW / REJECT
→ P0/P2 直接形成最终成员
→ P1 按 L1 缺口从 cluster 索引补采并再次做有效性审核
→ 补采字段投影、最终选择与 *_screened.jsonl 发布
```

其中两部分的职责分别是：

| 组件 | 主要职责 | 在旧链路中的位置 |
| --- | --- | --- |
| P012build | 固定特征、P012 初始采样、八项质量标签 | 上游数据集生成器 |
| Screening v1 | 分数区间预选、有效性审核、P1 补采、最终筛选与发布 | 下游二次筛选器 |

### 2.2 当前基线：Screening v2

v2 直接读取 cluster 和 taxonomy，不再读取旧 labeled 数据，也不调用 P012build。
P012build 的有效采样思想和八项质量维度已经在 `screening` 内重新实现，但运行时
不依赖旧代码。

v2 的稳定生产入口为：

```powershell
python -m screening.run_pipeline
```

## 3. 总体架构对比

| 维度 | v1 + P012build | v2 | 变化结论 |
| --- | --- | --- | --- |
| 系统形态 | 两套程序串联 | 单一闭环管线 | 消除阶段断裂 |
| 样本事实源 | 初始样本来自 labeled；cluster 主要供 P1 补采 | cluster 是 P0/P1/P2 唯一样本来源 | 来源口径统一 |
| 类别事实源 | P012build 和 v1 各自读取 taxonomy；初始 labeled 自带 L1/L2 | taxonomy 是唯一 L1/L2 来源 | 避免标签漂移 |
| 主要入口 | P012 构建、P012 标注、Screening v1 分别执行 | `python -m screening.run_pipeline` | 操作面收敛 |
| 处理顺序 | 采样 → 两轮质量标签 → 分数预选 → 有效性审核 | 采样 → 第一阶段准入 → 第二阶段标签 | 准入与标签职责明确 |
| 数据契约 | 早期即形成 flat labeled 字段 | 内部 canonical，末端导出 legacy | 语义与兼容格式解耦 |
| 大文件处理 | P012build 将 cluster 全量载入内存；v1 仅补采部分使用 SQLite | 全链路流式扫描、轻量 SQLite、定点物化 | 可扩展到大文件 |
| P1 配额层级 | P012 初采按 L2；v1 最终补采按 L1 | 从初采到补采始终冻结 L2 配额 | 配额口径一致 |
| LLM 调用单位 | P012 默认每批 10 条并自适应拆分；v1 有效性按单条并发 | 两个阶段均一条样本一次请求、全局并发 | 响应归属更可靠 |
| 初始/补采质量一致性 | 不一致 | 完全一致 | 解决补采字段与标签缺失 |
| 第二阶段作用 | `overall_score` 会被 v1 用于候选预选 | 只做难度及综合质量标签，不淘汰 | 质量分不再反向决定准入 |
| 集合关系 | P1 补采显式排除三个初始集合已用样本 | 三个集合独立选样，允许跨集合重复 | 保留各集合独立语义 |
| LLM 结果复用 | 两套工具分别缓存或恢复，无法跨集合统一复用 | 同一等价任务在 run 内跨集合复用 | 减少重复调用 |
| 错误处理 | P012 标注会做部分默认修正；两套账本分离 | 严格 Schema、矛盾响应重试、工程失败阻断发布 | 避免静默降级 |
| 发布 | P012 产出 labeled，v1 再产出 screened | 统一验证后发布三份 labeled v4 | 下游只接收一种交付 |
| 运行恢复 | P012 与 v1 各自恢复 | run identity 覆盖输入、规则、Prompt、模型和导出版本 | 恢复边界完整 |

## 4. 各处理阶段的详细对比

### 4.1 输入与事实源

#### v1 + P012build

P012build 先读取 cluster 与 taxonomy，生成三个初始数据集。Screening v1 随后需要：

- 三份已经由 P012build 生成并标注的 labeled 文件；
- taxonomy；
- cluster 全集；
- v1 有效性审核 Prompt；
- v1 配置和模型配置。

因此，同一轮数据生产同时存在三种事实来源：cluster 原始记录、P012build 生成的 flat
记录、Screening v1 最终 screened 记录。初始样本以 labeled 内容为准，而补采样本
又回到 cluster 读取，天然形成两种来源路径。

#### v2

v2 正式输入只有：

- cluster JSONL：全部候选样本的唯一来源；
- taxonomy JSON：L1/L2 及 population 的唯一来源；
- 版本化业务配置、两个 Prompt 和模型公开参数。

旧 labeled 文件不再是输入。所有最终字段都能追溯回同一条 cluster 源记录、同一份
taxonomy 和同一个 v2 run contract。

### 4.2 输入校验与大文件处理

#### v1 + P012build

P012build 使用 `defaultdict(list)` 将非噪音 cluster 记录全部载入内存，再按 cluster
计算频率分布、特征和采样。这种实现简单，但 cluster 很大时会显著增加内存占用。

Screening v1 后续为 P1 补采实现了流式扫描和 SQLite 索引，但它只覆盖补采部分，
无法改变 P012build 已经全量加载过一次的事实。两套程序还会分别读取、验证和理解
同一份 cluster。

#### v2

v2 在任何 LLM 调用前完成一次统一扫描：

1. 二进制顺序读取 JSONL；
2. 保存行号、字节偏移、字节长度和行哈希；
3. 校验字段类型、范围、会话和 taxonomy 映射；
4. 累计 cluster 频率统计；
5. 将采样所需的 signals/features 写入 SQLite；
6. 被选中后才按 byte locator 回读并物化完整 canonical。

正文不会全量复制进 SQLite。索引支持 checkpoint、中断恢复、源文件变化检测和
SQLite 完整性检查。

错误被划分为三种明确结果：

- `cluster=-1`：正常噪音排除；
- 单行字段或映射问题：脱敏隔离并继续；
- JSON/UTF-8 损坏、taxonomy 冲突、identity 冲突、源文件扫描中变化或 SQLite
  损坏：终止整个构建。

### 4.3 会话语义、identity 与追溯

两版都以 cluster `text` 中的 `|` 表示多轮会话，并以最后一轮作为目标 Query。
v2 保留了这一业务语义，同时将其正式写入 canonical 契约：

- `full_text`：规范化后的完整会话；
- `context_turns`：目标轮之前的各轮数组；
- `target_query`：清理规定标记后的最后一轮；
- `turn_count`、`sample_type`：由有效轮次确定。

v2 的增强点包括：

- 只按完整多模态/ASR 标记匹配，修复 P012build 使用字符类正则导致的误删风险；
- `raw_query` 和 `original_turn_texts` 仅用于追溯，不再参与正式会话重建；
- 新增 `source_identity_sha256`，用于源记录去重和冲突检测；
- 新增 `conversation_sha256`，用于等价 LLM 任务缓存；
- 完整保存 source snapshot、源行、字节位置、行哈希和可选 trace 字段；
- 相同 identity 但 target 或 cluster 不同的记录直接终止构建，不进行猜测性合并。

### 4.4 固定字段计算

v2 延续了 P012build 的主要业务含义：

- `cluster_prob` 缺失取 `0.0`；
- `frequency` 缺失取 `1`；
- probability 分段仍为 `0.9 / 0.5`；
- frequency 仍按 cluster 内严格小于当前值的比例分为 hot/warm/cold；
- `log1p(frequency)` min-max 归一化；
- P0 relevance 仍为 `0.6 × probability + 0.4 × normalized_frequency`；
- P2 relevance 仍为两个反向指标各占 `0.5`；
- 长度、复杂度和正式度的历史阈值保持不变。

关键区别是字段生成时机和保存方式：

| 项目 | v1 + P012build | v2 |
| --- | --- | --- |
| 固定字段生成 | P012build 在全量加载后直接写入 flat 数据 | 索引阶段计算，物化时写入 canonical |
| 数值精度 | `norm_freq`、relevance 提前 round 4 位 | canonical 保留原始精度，legacy 导出时 round 4 位 |
| 补采字段 | v1 尽量映射，无法获取时写 `null` | 与初始候选使用同一固定字段构建函数 |
| 字段校验 | 主要依赖脚本流程 | canonical 严格字段、类型、枚举和未知字段校验 |

### 4.5 P0/P1/P2 采样

#### P0：冒烟集

P0 的核心采样策略基本复用 P012build：

- 没有全局目标数量；
- 大 L1 取 3 个 L2，小 L1 取 2 个；
- 每个 L2 取规模最大的 cluster；
- 优先 high probability；没有 high 时取 relevance 最高的 10 条作为回退池；
- MMR `lambda=0.7`，每个 cluster 最多 5 条；
- 集合内字符二元组 Jaccard 去重阈值为 `>0.7`。

差异在准入环节：旧链路还要经过 `quality_overall_score ∈ [2,5]` 的预选和 v1
`KEEP` 判断；v2 删除分数区间预选，统一要求第一阶段四项全部通过。

#### P1：基准集

| 项目 | v1 + P012build | v2 |
| --- | --- | --- |
| P012 初始目标 | 3,000 | 不再单独存在 |
| v1 最终目标 | 默认取 P1 初版合法记录数，可配置 | 固定目标 10,000 |
| 初始 L2 权重 | cluster 实际观测行数的平方根 | taxonomy 声明 L2 population 的平方根，并受实际容量约束 |
| cluster 内采样 | probability × frequency 九宫格 + MMR | 延续九宫格权重、cold+boundary 保底和 MMR |
| v1 最终配额 | 按 L1 population 冻结和补采 | 按 L2 population 冻结并全程保持 |
| 补采 | 仅 P1，按 L1 缺口 | 仅 P1，按 L2 缺口 |
| 补采质量处理 | 只经过 v1 有效性审核 | 与初始样本一样先经过第一阶段；最终成员统一经过第二阶段 |
| 补采字段 | 投影到初始 labeled Schema，部分字段可能为 `null` | 完整 canonical 后统一导出，无补 `null` |

v2 仍保留三轮补采、默认通过率 `0.55`、先验强度 `10`、通过率区间
`[0.10, 0.95]` 和安全系数 `1.1`。变化的重点不是补采公式，而是把配额单位从 v1
最终阶段的 L1 下沉到 L2，并让初始与补采使用完全相同的候选、质量和字段契约。

#### P2：对抗集

两版都使用以下核心候选门槛：

```text
cluster_probability < 0.7
frequency 不高于所在 cluster 的 0.5 分位门槛
complexity >= moderate
```

两版也都采用单轮/多轮 `60% / 40%` 双轨、按 L2 合格候选量平方根分配、
`relevance_p2` 排序和 `>0.75` 的集合内 Jaccard 去重。

主要变化如下：

- P012build 默认 P2 目标为 500；v2 提升为 3,000；
- 旧链路先要求质量分 3～5，再接纳 v1 的 `KEEP` 或
  `TESTABLE_DIFFICULTY REVIEW`；
- v2 不再保留 REVIEW 旁路，所有 P2 成员也必须满足第一阶段
  `exact + self_contained + clear + valid`；
- v2 的第二阶段难度和综合质量结果只做标签，即使标为简单或低分也不会再淘汰；
- P2 仍不补采，候选不足或第一阶段淘汰导致的缺口如实记录。

这意味着 v2 的“对抗性”主要由低置信、低频和复杂度候选策略产生，而不是通过放宽
基础有效性或类别归属标准产生。

### 4.6 两阶段 LLM 与准入语义

#### 旧链路的实际调用

旧链路对初始样本存在三次逻辑判断：

1. P012build 历史 Pass 1：D1～D4；
2. P012build 历史 Pass 2：D5～D8；
3. Screening v1：`KEEP / REVIEW / REJECT` 有效性审核。

P012build 默认一次请求包含 10 条样本，结构失败后逐步拆半，直到单条。输入包含
`index`、`text`、`raw_text`、轮次字段、L1/L2；模型需要按 index 返回数组。

Screening v1 的有效性审核采用单条输入，但只包含：

```text
sample_type
context_turns
target_query
```

它明确不参考 L1/L2，因此不能承担类别归属检测。P1 补采样本只走这一步，不会再走
P012build 的 D1～D8，形成初始与补采质量处理不对称。

此外，旧 P012build 的第二轮综合分会在 Screening v1 入口处作为预选条件：P0
允许 2～5 分，P1/P2 允许 3～5 分。因此它虽然名义上是质量标签，实际上也参与了
成员淘汰。

#### v2 的统一调用

v2 只保留两个正式阶段，两阶段都采用“一条样本一次请求”和全局并发池。

第一阶段统一称为“有效性、基础质量及类别检测”。输入只有：

```text
sample_type
context_turns
target_query
L1
L2
```

输出只有：

```text
category_fit
semantic_completeness
intent_clarity
validity
```

三个集合的准入条件完全一致：

```text
exact + self_contained + clear + valid
```

第二阶段统一称为“难度及综合质量”。它读取相同五个原始字段和第一阶段四项结果，
仅输出：

```text
classification_difficulty
task_difficulty
issue_types
overall_score
```

第二阶段只对最终成员执行，只补标签，不改变任何集合的成员。合法的低分、简单或
困难标签都不会触发淘汰；但缺少合法第二阶段结果的数据不允许作为完整成品发布。

#### 调用协议变化的意义

- 单条请求消除了批量返回漏项、错序、复制 index 和样本间互相污染的风险；
- 输入只提供判断必需字段，减少无关原始字段对模型的干扰；
- 输出必须恰好符合白名单，模型复写输入字段或返回多余字段会被拒绝并重试；
- 分类归属和基础有效性在同一阶段判断，taxonomy 错配不能绕过准入；
- 第二阶段四项标准被保留，但从“隐式筛选依据”回归“最终样本描述标签”。

### 4.7 去重与跨集合关系

#### v1 + P012build

- P012build 先在 cluster 内按正式输入文本精确去重；
- 各集合采样后再做 Jaccard 去重；
- Screening v1 的 P1 补采会排除 P0/P1/P2 初始文件已经使用的样本；
- P012build 与 Screening v1 使用不同身份定义和账本。

#### v2

- `source_identity_sha256` 负责识别同一源记录；
- `conversation_sha256` 负责识别等价 LLM 任务；
- P0/P1/P2 各自在集合内去重；
- 不做跨集合成员去重，因为三个集合不是把样本全局分配给唯一去向，而是分别判断
  某个样本是否适合自身目标；
- 同一等价样本被多个集合选中时可以分别成为成员，但 LLM 结果只调用和缓存一次。

### 4.8 数据契约与 28 字段输出

#### v1 + P012build

P012build 在采样阶段就生成接近最终格式的 flat 字段，质量标注再追加八个
`quality_*` 字段。Screening v1 以这些 labeled 字段作为目标 Schema：

- 初始记录保持原字段和值；
- P1 补采记录再投影到该 Schema；
- 能确定的字段进行映射；
- 无法可靠生成的字段写 `null`。

这使“字段格式一致”不等于“字段语义和完整性一致”。尤其是补采记录并未真正经过
生成这些字段所代表的处理。

#### v2

v2 将数据分成两个层次：

1. `screened.canonical.jsonl`：内部权威事实源，包含完整 candidate、八项质量结果、
   集合、初始/补采来源、补采轮次和三个任务 ID；
2. `*_labeled.jsonl`：面向当前下游的 28 字段兼容视图。

legacy exporter 不调用 LLM、不采样、不改变成员，只做确定性映射。`norm_freq` 和
relevance 到导出阶段才保留四位小数；P0/P1 输出 `relevance_p0`，P2 输出
`relevance_p2`。除 `context_text` 可为空字符串外，所有字段必须存在、类型正确且
不得为 `null`。

因此，v2 的兼容字段是完整处理结果的投影，不再是为了匹配 Schema 而填充出来的
占位结构。

### 4.9 缓存、恢复、审计与失败语义

| 项目 | v1 + P012build | v2 |
| --- | --- | --- |
| 恢复边界 | P012 标注和 Screening v1 分别恢复 | 一个 v2 run contract |
| LLM 账本 | 两套程序分别维护 | 两阶段 task/attempt/result 统一维护 |
| 缓存键 | 依赖各工具自己的 identity/signature | 阶段、Prompt、模型参数、conversation、taxonomy、第一阶段结果 |
| 跨集合复用 | 无统一保证 | 等价任务跨集合只调用一次 |
| 非法响应 | P012 存在 normalize/clamp 和规则修正 | 多余字段、非法枚举和逻辑矛盾均作为响应错误重试 |
| 工程失败 | 各工具分别产生失败文件 | 未完成任务使 run incomplete，退出码 2，可恢复但禁止发布 |
| 审计原文 | 旧失败账本可能保存原始 case | manifest、审计和错误账本不写 Query 原文 |
| 来源追溯 | v1 重点覆盖 P1 最终补采 | 三个集合每条记录均关联 source/candidate/stage1/stage2 |

v2 run identity 覆盖源文件快照、taxonomy 哈希、canonical/index/feature 版本、采样和
补采配置、两个 Prompt 哈希、模型公开参数、质量协议、screened schema 和 legacy
mapping 版本。历史 v1 run 与 manifest 明确不兼容，不会被错误恢复。

### 4.10 发布与最终产物

旧链路存在两层主要数据文件：P012build 的 `*_v4_labeled.jsonl` 和 Screening v1
最终的 `*_screened.jsonl`。使用方需要明确自己消费的是哪一层结果。

v2 的下游交付固定为：

```text
P0-冒烟集/P0_smoke_set_v4_labeled.jsonl
P1-基准集/P1_benchmark_set_v4_labeled.jsonl
P2-对抗集/P2_adversarial_set_v4_labeled.jsonl
```

内部 canonical、任务账本、provenance、manifest 和报告全部保留在 run 工作区，不
复制到发布目录。每次完整批跑还会自动生成 `screening_run_report.json` 和
`screening_run_report.md`：前者用于机器读取和版本对比，后者可直接用于汇报，覆盖
P0/P1/P2 的 L1/L2 语义树分布、P1 配额完成情况、Query 基础可用率、语义标签一致率、
Stage1 准入率、补采波次及最终难度/质量分布。其中语义标签一致率是 Stage1
LLM-as-judge 结果，不等同于人工金标准确率。

发布前会独立复验：

- 源文件、索引、taxonomy 和 Prompt 未变化；
- 两阶段任务完整；
- screened canonical 与任务账本一致；
- P1 配额、最终数和缺口守恒；
- 批跑报告与候选任务、Stage1 结果、最终 L2 分布及 Markdown 渲染一致；
- legacy 字段名称、顺序、类型、枚举和逐条映射正确；
- provenance 记录的 source/canonical/legacy 哈希与实际文件一致；
- manifest 的路径、哈希和行数与磁盘一致。

三个文件作为一个事务提交。不同内容默认拒绝覆盖；显式 `--replace` 时任一文件
提交失败会回滚整组目标。

## 5. 三个集合的最终行为对比

| 集合 | v1 + P012build 最终行为 | v2 最终行为 |
| --- | --- | --- |
| P0 | P012 结构采样，无全局上限；质量分 2～5 进入 v1 审核；只保留 KEEP；不补采 | 延续结构采样，无全局上限；第一阶段四项满格才准入；第二阶段只标注；不补采 |
| P1 | P012 初始目标 3,000；v1 以初版合法量为默认目标，按 L1 population 配额；KEEP 进入池；按 L1 缺口补采 | 最终目标 10,000；按 taxonomy L2 population 冻结配额；第一阶段通过即准入；按 L2 缺口最多补采三轮；第二阶段统一补标签 |
| P2 | P012 初始目标 500；低置信、低频、复杂双轨采样；质量分 3～5；接纳 KEEP 和可测困难 REVIEW；不补采 | 目标 3,000；延续低置信、低频、复杂双轨采样；只接纳第一阶段四项满格样本；第二阶段难度不淘汰；不补采 |

以下原则在 v2 中对三个集合统一成立：

- 分支独立采样，不做全局样本分配；
- 集合内去重，不做跨集合去重；
- 第一阶段是唯一业务质量准入门槛；
- 第二阶段不改变成员；
- 工程失败禁止发布，业务数量缺口可以如实发布；
- 最终成员必须具有完整八项质量标签和完整 28 字段兼容输出。

## 6. 从 v1 到 v2 具体完成了什么

### 6.1 链路整合

- 将 P012build 的固定字段计算和 P0/P1/P2 采样能力重新实现在 screening 内；
- 删除 v2 对旧 labeled 输入和旧 P012build 运行时的依赖；
- 将原来的多个命令收敛为一个 v2 总入口；
- P0/P1/P2 从同一个 canonical index 独立运行。

### 6.2 质量体系重构

- 统一为两个正式阶段，不再沿用 `KEEP / REVIEW / REJECT` 作为 v2 协议；
- 第一阶段同时判断有效性、基础质量和 L1/L2 类别归属；
- 明确 `exact + self_contained + clear + valid` 为三个集合的统一准入条件；
- 第二阶段保留分类难度、任务难度、问题类型和综合质量能力，但不再参与业务淘汰；
- 改为一次一条、并发调用，严格限制输入输出字段；
- 最终成员缺少第二阶段结果时禁止发布。

### 6.3 P1 配额与补采重构

- 将默认最终目标明确为 10,000；
- 将 v1 最终阶段的 L1 配额改为 L2 配额；
- 配额使用 taxonomy 声明 population，并结合实际候选容量；
- 初始采样后冻结 L2 目标，补采期间不漂移；
- 只允许 P1 补采，最多三轮；
- 初始和补采使用同一第一阶段标准，补采完成后统一执行第二阶段；
- 完整记录每轮请求量、通过率、去重、准入、截断和逐 L2 缺口。

### 6.4 数据契约重构

- 建立版本化 canonical candidate；
- 明确 source、conversation、taxonomy、signals、features 的语义边界；
- 增加 source identity、conversation identity、字节 locator 和源行哈希；
- 修正多模态/ASR 标记的字符类正则缺陷；
- 固定特征不再因补采来源不同而缺失；
- 建立 screened canonical 作为内部权威成品；
- 将 legacy 28 字段降为最终兼容映射，不再反向约束内部处理。

### 6.5 大文件与工程能力重构

- 用流式扫描和 SQLite 索引替代 P012build 的 cluster 全量内存加载；
- 通过 byte locator 延迟物化正文；
- 增加索引版本、特征规则版本、taxonomy 哈希和源快照兼容判断；
- 增加单行隔离、致命冲突、SQLite 完整性和源文件变化检查；
- 建立统一 LLM 缓存、任务账本和 run 恢复机制；
- 审计数据脱敏，不在错误与 manifest 中记录 Query 原文。

### 6.6 最终交付与发布重构

- 每条最终记录都能追溯到源行、candidate 任务和两个 LLM 阶段任务；
- 独立校验 canonical、legacy、provenance、配额和 manifest；
- 固定发布三份下游兼容的 labeled v4 文件；
- 支持内容相同时幂等发布；
- 不同内容默认拒绝覆盖；
- 显式替换采用三文件事务并在失败时回滚。

### 6.7 代码与资料治理

- v2 代码按 `candidate / quality / pipeline / delivery / shared` 分层；
- `screening` 主包只保留 v2 可执行代码；
- 删除旧链路功能代码、兼容转发和旧功能测试；
- v0/v1 技术文档、Prompt 和配置集中归档；
- P012build 保持原位，仅作为历史实现参考，不参与 v2 运行。

## 7. 保留和复用的旧版能力

v2 并非推翻全部旧策略。以下经过验证的业务思想被保留：

- cluster `text` 的多轮拆分语义；
- probability/frequency 分段和 `log1p` 归一化；
- P0/P2 relevance 公式；
- 长度、复杂度和正式度的主要规则及阈值；
- P0 的 L1/L2/cluster 结构采样、high 回退和 MMR；
- P1 的 cluster 平方根配额、九宫格权重、cold+boundary 保底和 MMR；
- P2 的低置信、低频、复杂度门槛与 60/40 双轨；
- 字符二元组 Jaccard 集合内去重；
- 只有 P1 补采以及三轮、自适应通过率的补采思想；
- 两阶段八项质量维度及其核心枚举；
- 当前下游依赖的 28 字段 labeled v4 格式。

因此，v2 的本质是：**保留有效的业务选择逻辑，重构数据来源、处理顺序、质量语义
和工程保障。**

## 8. 汇报视角下的收益

### 8.1 数据质量

- 初始与补采样本经过完全相同的准入和标签流程；
- 所有最终记录都具备完整且真实生成的固定字段和八项质量标签；
- taxonomy 类别错配在准入阶段即可被发现；
- 不再通过补 `null` 伪装字段对齐；
- 第二阶段低分不再与“样本是否有效”混为一谈。

### 8.2 覆盖与集合定位

- P1 从 L1 粗粒度补齐升级为 L2 细粒度配额和补采；
- P1 目标从旧 P012 默认 3,000 提升为最终 10,000；
- P2 目标从 500 提升为 3,000；
- P0 保持小规模、结构化、高置信冒烟定位；
- 三个集合独立选样，避免全局分配损害各集合自身目标。

### 8.3 成本与效率

- 第二阶段只对最终准入成员调用，避免为最终未采用的过量候选打难度标签；
- 等价任务跨集合共享缓存；
- P1 补采只对去重后的新候选调用第一阶段；
- 流式索引避免重复全量装载 cluster。

需要注意：v2 的 P1/P2 目标数量显著提高，因此不能仅根据调用结构断言总 LLM 成本
一定低于旧版。准确结论应在真实 run 后结合候选量、第一阶段通过率、跨集合重复率和
补采轮次统计。但在相同候选规模下，v2 消除了旧初始样本“P012 两轮 + v1 再审核”
的重复判断，并避免对最终未入选样本调用第二阶段。

### 8.4 可追溯与可运营

- 任一输出记录都能定位到 cluster 原始字节位置；
- 每个模型任务都有冻结输入身份、attempt 和合法结果；
- 业务缺口与工程失败被明确区分；
- 同一输入和配置可恢复或复用，输入变化不会误续跑；
- 三份最终文件要么全部成功发布，要么全部保持原状。

## 9. 需要关注的行为变化与风险

| 变化/风险 | 说明 | 建议关注指标 |
| --- | --- | --- |
| P1 目标扩大 | 从旧 P012 默认 3,000 转为最终 10,000 | L2 覆盖、补采轮次、最终缺口、LLM 成本 |
| P2 目标扩大 | 从 500 转为 3,000 | 固定门槛候选容量、第一阶段通过率 |
| P2 不再接纳 REVIEW | 严格统一为第一阶段四项满格 | P2 对真实类别边界样本的保留率 |
| taxonomy 依赖更强 | L1/L2 唯一来源且 P1 使用声明 population | taxonomy 映射冲突、population 缺失或失真 |
| source 契约更严格 | session/text 必须有效，类型错误会隔离或终止 | 隔离率、问题码分布、源数据修复量 |
| 不做跨集合去重 | 同一源样本可同时进入多个集合 | 跨集合重叠率及是否符合评测消费方式 |
| P1/P2 允许业务缺口 | 不降标、不复制、不跨 L2 伪造补齐 | 最终数量、逐 L2 缺口和停止原因 |
| 单条 LLM 请求 | 结果可靠性更高，但请求数更多 | 吞吐、首 Token、P95 时延、限流率 |
| legacy 仍有限制 | 当前 28 字段要求整数 cluster ID | 字符串 cluster ID 的输入比例 |

这些变化不是实现缺陷，而是新版明确选择的业务边界。正式大规模运行后，应使用 v2
manifest、branch audit、P1 supplement summary 和 LLM attempts 对上述指标做首轮
基线评估。

## 10. 汇报版总结

### 10.1 旧链路的问题

- 数据先在 P012build 构建和标注，再到 Screening v1 二次筛选，职责重复；
- 初始样本来自 labeled，补采样本来自 cluster，两条数据路径不一致；
- 补采样本没有完整经历固定特征和两阶段质量标注，部分字段只能补 `null`；
- 质量分、有效性、难度和集合准入互相耦合；
- P1 初采按 L2、最终补采按 L1，配额口径前后不一致；
- P012build 全量加载 cluster，不适合超大源文件；
- 两套运行、缓存、失败账本和发布产物增加了维护与操作成本。

### 10.2 v2 做出的改造

- 以 cluster 为唯一来源，以 taxonomy 为唯一类别来源；
- 把旧 P012 的有效采样能力重写进 screening；
- 建立 canonical 数据契约、流式索引和按需物化；
- 用第一阶段统一决定有效性、基础质量和类别准入；
- 用第二阶段统一补充难度及综合质量，不再淘汰；
- 只有 P1 按冻结 L2 配额补采，初始和补采同标准处理；
- 最终统一生成 screened canonical，再确定性映射 28 字段；
- 建立完整任务账本、缓存、恢复、独立复验和事务发布。

### 10.3 最终价值

一句话概括：

> Screening v2 将原来“先生成、再筛选、补采再补字段”的两段式链路，升级为一条
> 从 cluster 到最终 labeled 数据的统一、可恢复、可追溯生产管线。

对下游而言，文件格式仍是熟悉的 28 字段 labeled v4；对内部而言，数据来源、准入
标准、补采口径和质量标签第一次在同一套契约下闭环。

## 11. 关键参数对照

| 参数 | v1 + P012build | v2 |
| --- | ---: | ---: |
| P0 全局目标 | 无 | 无 |
| P0 每 cluster 上限 | 5 | 5 |
| P0 Jaccard 阈值 | 0.7 | 0.7 |
| P1 初始/最终目标 | P012 3,000；v1 默认取初版合法量 | 最终 10,000 |
| P1 Jaccard 阈值 | 0.75 | 0.75 |
| P1 补采轮数 | 最多 3 | 最多 3 |
| P1 默认通过率 | 0.55 | 0.55 |
| P1 先验强度 | 10 | 10 |
| P1 通过率边界 | 0.10～0.95 | 0.10～0.95 |
| P1 安全系数 | 1.1 | 1.1 |
| P2 目标 | 500 | 3,000 |
| P2 多轮比例 | 0.4 | 0.4 |
| P2 probability 上限 | `<0.7` | `<0.7` |
| P2 frequency 分位 | `<=0.5` | `<=0.5` 的精确排名实现 |
| P2 最低 complexity | moderate | moderate |
| P2 Jaccard 阈值 | 0.75 | 0.75 |
| MMR lambda | 0.7 | 0.7 |
| LLM workers | P012 批量；v1 全局 2 | 全局 2，可配置 |
| LLM 最大重试 | 3 | 3，可配置 |

## 12. 资料依据

本报告依据当前工作区中的以下实现与技术资料整理：

- v2 技术方案：`screening/artifacts/docs/screening_pipeline_technical_design_v2.md`；
- v2 默认配置：`screening/artifacts/config/pipeline.default.json`；
- Screening v1 归档技术方案：
  `screening/artifacts/archive/v1/docs/screening_pipeline_technical_design_v1.md`；
- Screening v1 归档配置与有效性 Prompt：
  `screening/artifacts/archive/v1/configs/`、`screening/artifacts/archive/v1/prompts/`；
- P012build 旧版说明、实际生成脚本、质量标注脚本、Prompt 与配置：
  `D:\wwc\p012build`。

对旧链路的描述采用“P012build 输出作为 Screening v1 初始 labeled 输入”的组合口径；
如果只单独比较其中一个组件，会遗漏初始构建或最终二次筛选，不能代表旧版从 cluster
到最终输出数据集的真实全链路。
