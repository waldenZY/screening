# Screening v2 技术方案

> 文档状态：当前实现基线  
> 适用入口：`python -m screening.run_pipeline`  
> 更新日期：2026-09-12

## 1. 目标与边界

Screening v2 从统一的 cluster JSONL 和 taxonomy JSON 出发，独立构建 P0、P1、P2
三套数据。系统先建立可恢复的 canonical 索引，再按三个集合各自的规则选择候选，
经过两个 LLM 阶段，形成内部权威的 screened canonical，最后映射为下游依赖的
28 字段 labeled v4 文件。

完整链路如下：

```text
cluster JSONL + taxonomy JSON
→ 流式扫描、校验和 canonical 索引
→ P0 / P1 / P2 独立采样与集合内去重
→ 第一阶段：有效性、基础质量及类别检测
→ 第一阶段通过即准入
→ 仅 P1 按冻结 L2 配额补采
→ 第二阶段：难度及综合质量标签
→ screened canonical
→ legacy labeled v4 确定性映射
→ JSON/Markdown 批跑报告
→ 全量一致性校验
→ 三文件事务发布
```

v2 的运行边界为：

- cluster 是全部候选样本的唯一数据来源；taxonomy 是 L1/L2 的唯一类别来源。
- P0、P1、P2 是三条独立链路。一个源样本可以同时被多个集合选中。
- 每个集合内部去重，不进行跨集合去重。
- 第一阶段是三个集合统一的业务准入门槛。
- 第二阶段只补充标签，不改变集合成员。
- 只有 P1 补采；P0 和 P2 始终只有初始波次。
- canonical screened 是内部事实源；legacy labeled 是当前下游交付格式。
- 批跑报告只聚合脱敏统计，不保存 Query 原文，也不增加 LLM 调用。
- v2 不读取旧 labeled 数据作为输入，也不恢复 v1 run 或旧 manifest。

## 2. 设计原则

### 2.1 语义与兼容格式分离

内部数据使用语义清晰、版本化的嵌套 canonical 契约。旧字段名只在最终 exporter
中出现，避免 `relevance_p0`、`quality_*` 等兼容名称反向约束新链路的内部语义。

### 2.2 决策与标注分离

第一阶段决定样本是否准入；第二阶段负责难度和综合质量描述。第二阶段的任意合法
结果都不会再淘汰样本，因此成员确定逻辑只有一处。

### 2.3 定位信息与正文分离

索引保存候选筛选需要的统计、特征和原文件字节位置，不保存完整会话文本。候选被
某一分支选中后，系统才按字节位置定点读取源行并物化完整 canonical。

### 2.4 确定性与可恢复性

配额、排序、去重、哈希和导出均为确定性操作。LLM 结果使用 run-local 缓存和任务
账本；同一输入、配置、Prompt 和模型契约对应同一个 v2 run identity。

### 2.5 发布前独立复验

发布不信任构建过程的内存状态，而是从磁盘重新读取 canonical、legacy、provenance、
任务账本和 manifest，独立验证后才允许提交下游文件。

## 3. 输入契约

### 3.1 Cluster JSONL

源文件每行必须是一个 UTF-8 JSON 对象。构建所需的核心字段为：

| 字段 | 要求 | 用途 |
| --- | --- | --- |
| `cluster` | 与 taxonomy 一致的整数或字符串 | 类别映射与 cluster 统计 |
| `text` | 非空字符串 | 正式会话构建和源 identity |
| `session` | 非空字符串 | `session_id` 和源 identity |
| `cluster_prob` | `[0, 1]` 有限数值；可缺失 | cluster 置信度，缺失时取 `0.0` |
| `frequency` | 非负整数；可缺失 | 频率信号，缺失时取 `1` |

以下字段只用于追溯，允许缺失并在 canonical 中保存为 `null`：

```text
sn
raw_index
seg_id
turn_range
raw_query
original_turn_texts
boundary_confidence
topic_hint
```

完整源对象不会嵌入 canonical。系统通过源快照、行号、字节偏移、字节长度、行哈希
和追溯字段定位原始记录。

### 3.2 Taxonomy JSON

taxonomy 顶层必须包含非空 `categories` 数组。每个 L1 需要提供：

- `name`：L1 名称；
- `total_size`：L1 声明规模；
- `assigned_clusters`：属于该 L1 的 cluster；
- `sub_intents`：L2 数组。

每个 L2 需要提供：

- `name`：L2 名称；
- `size`：L2 声明 population；
- `cluster_ids`：属于该 L2 的 cluster。

系统以 `L1/L2` 生成稳定的 `l2_key`。taxonomy 中的 cluster ID 必须统一为整数或
统一为字符串，且一个 cluster 不得映射到多个 L1 或多个 L2。缺少 L2 映射的源行
不能进入候选池。

P1 的 L2 配额使用 taxonomy 声明的 L2 population；cluster 实际观测数量用于容量
限制、cluster 内配额和审计。P2 的 L2 配额使用通过其固定门槛后的实际候选数量。

## 4. Canonical candidate 契约

当前版本：

```text
canonical_schema_version = 1
canonical_index_version = 2
feature_rule_version = 2
```

完整 candidate 的结构如下：

```json
{
  "schema_version": 1,
  "source_identity_sha256": "...",
  "conversation_sha256": "...",
  "source": {
    "source_snapshot_id": "...",
    "source_line": 1,
    "byte_offset": 0,
    "byte_length": 100,
    "record_sha256": "...",
    "session_id": "...",
    "raw_text": "...",
    "trace": {
      "sn": null,
      "raw_index": null,
      "seg_id": null,
      "turn_range": null,
      "raw_query": null,
      "original_turn_texts": null,
      "boundary_confidence": null,
      "topic_hint": null
    }
  },
  "conversation": {
    "full_text": "...",
    "context_turns": [],
    "target_query": "...",
    "turn_count": 1,
    "sample_type": "single_turn"
  },
  "taxonomy": {
    "cluster_id": 1194,
    "l1": "系统工具",
    "l2": "设置闹钟",
    "l2_key": "系统工具/设置闹钟"
  },
  "signals": {
    "cluster_probability": 1.0,
    "frequency": 483
  },
  "features": {
    "probability_band": "high",
    "frequency_band": "hot",
    "normalized_frequency": 1.0,
    "relevance": {
      "p0": 1.0,
      "p2": 0.0
    },
    "length_band": "mid",
    "complexity": "simple",
    "formality": "formal"
  }
}
```

除 `source.trace` 下的八个追溯字段外，所有 canonical 字段必须存在、非空且满足严格
类型和枚举约束。对象还必须与 schema 精确匹配，不接受未知字段。

### 4.1 会话解析

`parse_conversation(text)` 使用以下 canonical 会话语义：

1. 按字面量 `|` 拆分 cluster `text`。
2. 每轮压缩连续空白并去除首尾空白。
3. 删除规范化后为空的轮次。
4. 最后一轮为 `target_query`，前序轮次为 `context_turns`。
5. 全部规范化轮次用 ` | ` 连接为 `full_text`。
6. 一轮记为 `single_turn`，两轮及以上记为 `multi_turn`。

仅从目标轮移除完整的多模态/ASR 标记：

```text
[((多模态)输入)]
[(多模态)]
[ASR]
[(图片)]
[(语音)]
```

标记匹配以完整字符串为单位，不会把普通字符当成标记内容。目标轮清理后为空时，
该源行被隔离。`raw_query` 和 `original_turn_texts` 仅供追溯，不参与正式会话构建。

### 4.2 两种 identity

- `source_identity_sha256`：对规范化后的 `session_id` 和源记录原始 `text` 计算，
  用于源记录去重、集合内成员识别和任务追溯。
- `conversation_sha256`：对 `context_turns` 和清理后的 `target_query` 计算，用于
  LLM 等价任务缓存。

同一 source identity 的重复记录如果 target 与 cluster 一致，候选只保留第一条，
但重复行仍计入 cluster frequency 统计；如果相同 identity 对应不同 target 或
cluster，则视为不可安全消解的冲突并终止索引构建。

## 5. 固定特征

固定特征由 `compute_fixed_features()` 计算，不调用 LLM。canonical 内保留原始浮点
精度，只有 legacy exporter 会按兼容要求四舍五入。

### 5.1 概率与频率

概率分段：

| 条件 | `probability_band` |
| --- | --- |
| `cluster_probability >= 0.9` | `high` |
| `0.5 <= cluster_probability < 0.9` | `mid` |
| `< 0.5` | `boundary` |

频率分位采用同 cluster 内“严格小于当前 frequency 的行数 / cluster 总行数”：

| 条件 | `frequency_band` |
| --- | --- |
| percentile `>= 0.8` | `hot` |
| `0.2 <=` percentile `< 0.8` | `warm` |
| percentile `< 0.2` | `cold` |

`normalized_frequency` 在 cluster 内对 `log1p(frequency)` 做 min-max 归一化。cluster
内所有 frequency 相同时取 `1.0`。

### 5.2 Relevance

```text
relevance.p0 = 0.6 × cluster_probability
             + 0.4 × normalized_frequency

relevance.p2 = 0.5 × (1 - cluster_probability)
             + 0.5 × (1 - normalized_frequency)
```

P0/P1 偏向高置信度、高频样本；P2 偏向低置信度、低频样本。

### 5.3 文本特征

- `length_band`：清理后长度 `<10` 为 `short`，`10..30` 为 `mid`，`>30` 为 `long`。
- `complexity`：按轮数、问号、数字、长度、连接词、有效轮次和动作词累计固定规则
  分数，`<=0.3` 为 `simple`，`<=1.5` 为 `moderate`，其余为 `complex`。
- `formality`：按语气词、口语字词、粗俗词、重复标点、重复字符和表情累计固定规则
  分数，`<=0.5` 为 `formal`，`<=1.5` 为 `mixed`，其余为 `colloquial`。

## 6. 流式 canonical 索引

索引默认位于：

```text
<output-root>/_canonical_cache/<cache-id>/
├── canonical_candidate_index.sqlite
├── canonical_metadata.json
└── canonical_input_audit.json
```

索引构建流程：

1. 以二进制方式顺序扫描 JSONL，记录行号、字节偏移、字节长度和源行 SHA-256。
2. 校验 source、会话、signals、trace 和 taxonomy 映射。
3. 将有效候选的筛选字段写入 SQLite；正文不进入索引。
4. 同时累计每个非 noise cluster 中类型合法或采用缺失默认值的 frequency；即使该行
   因会话、session 或 taxonomy 映射问题随后被隔离，也保持这一统计口径。一致的
   重复源行同样计入统计。
5. 扫描完成后按 cluster 计算精确排名、总数、min/max 和固定特征并批量回填。
6. 写入完整源文件 SHA-256、源文件 stat 和头尾采样指纹。
7. 使用 SQLite quick/integrity check 验证索引。

中断时通过 checkpoint 恢复扫描。恢复或复用前会确认源文件没有变化；可通过
`--verify-full-source-hash` 强制重新计算完整源 SHA-256。

`CanonicalCandidateIndex.iter_by()` 可按 L1、L2 或 cluster 读取带特征的 locator。
候选被选中后，`materialize()` 按 locator 定点读取源字节，重新验证行哈希，再调用
`build_canonical_candidate()` 生成完整对象。

缓存兼容性由以下内容共同决定：

- canonical index version；
- feature rule version 和固定特征配置；
- taxonomy SHA-256；
- 源文件快照。

## 7. 输入错误策略

### 7.1 排除但不视为错误

- `cluster = -1`：计为 noise 并排除，不进入 frequency 统计或候选池。

### 7.2 使用默认值并审计

- 缺失 `cluster_prob`：使用 `0.0`。
- 缺失 `frequency`：使用 `1`。

### 7.3 隔离单行并继续

- probability 类型非法、非有限或越界；
- frequency 非整数、为负数或超出 SQLite 整数范围；
- 缺少或非法的 session/text；
- 会话为空或目标轮清理后为空；
- trace 已提供但类型非法；
- 源行 cluster ID 类型与 taxonomy 不一致；
- cluster 缺少 L1/L2 映射。

### 7.4 终止整个索引构建

- taxonomy 重复或冲突映射；
- taxonomy 自身混用 cluster ID 类型；
- JSON 或 UTF-8 损坏；
- 扫描过程中源文件发生变化；
- source identity 内容冲突；
- SQLite 完整性失败。

输入审计只记录行号、cluster、稳定问题码和聚合计数，不记录 Query 原文。

## 8. P0/P1/P2 独立采样

三条分支从同一 canonical index 独立读取候选，并通过三个工作线程并发执行初始
采样。源样本不是先全局分配给某个集合，因此跨集合重叠是合法结果，并在构建汇总
中单独统计。

### 8.1 P0：冒烟集

P0 无全局目标数量，不补采，采用结构覆盖策略：

1. 按 taxonomy 的 L1 声明顺序遍历。
2. `L1.total_size >= 3000` 时选该 L1 声明规模最大的 3 个 L2，否则选 2 个。
3. 每个 L2 只使用实际观测规模最大的 cluster。
4. cluster 内优先使用 `probability_band=high` 的样本。
5. 如果该 cluster 没有 high 样本，使用 `relevance.p0` 最高的 10 条作为回退池。
6. 在候选池内以 `relevance.p0` 为质量项执行 MMR，`lambda=0.7`。
7. 每个 cluster 最多选 5 条。
8. 集合内执行字符二元组 Jaccard 去重；相似度 `>0.7` 视为重复，优先保留
   `cluster_probability` 更高者。

去重后的候选进入第一阶段，通过即成为 P0 成员。第二阶段只补标签。

### 8.2 P1：基准集

P1 的最终目标是第一阶段通过、集合内去重后的 10,000 条，最多补采三轮。

初始采样：

1. 对有候选的 L2 按 `sqrt(taxonomy L2 population)` 分配目标配额。
2. 配额受实际候选容量约束，使用确定性余数分配，并尽可能保证每个有候选 L2
   至少覆盖一条。
3. L2 内按 `sqrt(cluster observed size)` 分配 cluster 配额。
4. cluster 内按 probability × frequency 九宫格分配：
   - probability：`high=3`、`mid=2`、`boundary=1`；
   - frequency：`hot=2`、`warm=2`、`cold=1`；
   - `boundary+cold` 有候选时最低分配 1 条。
5. 每个网格内按 `relevance.p0` 执行 MMR，`lambda=0.7`。
6. 网格不足时按确定性 relevance 顺序回填剩余 cluster 配额。
7. 集合内字符二元组 Jaccard 相似度 `>0.75` 视为重复，优先保留
   `cluster_probability` 更高者。

第一阶段结束后，系统按 taxonomy population 和实际容量冻结最终 L2 目标配额。
初始通过样本超过某个 L2 配额时，只按确定性候选顺序保留配额内样本，未采用的
通过样本进入审计统计但不成为最终成员。

### 8.3 P2：对抗集

P2 初始采样目标为 3,000，不补采。固定候选门槛为：

```text
cluster_probability < 0.7
frequency_less_count <= round((cluster_frequency_count - 1) × 0.5)
complexity >= moderate
```

其中 `frequency_less_count` 是同 cluster 内严格小于当前 frequency 的行数。该表达式
是当前实现对 0.5 分位门槛的精确定义，能在频率并列时保持稳定结果。

采样分为两个轨道：

- 单轮目标 1,800；
- 多轮目标 1,200。

每个轨道按 L2 合格候选数的平方根分配配额，L2 内按 `relevance.p2` 降序选择，
并过滤相同会话文本。两个轨道合并后执行集合内字符二元组 Jaccard 去重；相似度
`>0.75` 视为重复，优先保留 `relevance.p2` 更高者。

P2 只执行一次初始采样。第一阶段淘汰或候选不足都可能使最终数量低于 3,000，缺口
如实记录，不伪造补足。第二阶段即使判为简单或低分，也不改变 P2 成员。

## 9. 两阶段 LLM 质量处理

两个阶段都采用“一条样本一次请求”的调用方式，并共享一个全局并发池。默认参数：

```text
workers = 2
max_retries = 3
sleep_seconds = 1.0
```

Prompt 分别冻结到 run 目录的 `prompts/stage1.md` 和 `prompts/stage2.md`。LLM 响应
只允许输出阶段白名单字段；多余字段、输入字段复写、非法枚举、缺字段或逻辑矛盾
都会作为响应错误重试。

### 9.1 第一阶段：有效性、基础质量及类别检测

输入严格限制为：

```json
{
  "sample_type": "single_turn | multi_turn",
  "context_turns": [],
  "target_query": "...",
  "L1": "...",
  "L2": "..."
}
```

输出严格限制为：

```json
{
  "category_fit": "exact | partial | mismatch",
  "semantic_completeness": "self_contained | context_dependent | fragments",
  "intent_clarity": "clear | vague | no_intent",
  "validity": "valid | borderline | invalid"
}
```

三个集合使用同一准入条件：

```text
category_fit = exact
AND semantic_completeness = self_contained
AND intent_clarity = clear
AND validity = valid
```

只要任一条件不满足，样本不会进入该集合的最终成员。

### 9.2 第二阶段：难度及综合质量

第二阶段输入为相同五个原始字段，加第一阶段四个结果。输出严格限制为：

```json
{
  "classification_difficulty": "trivial | easy | moderate | hard | not_applicable",
  "task_difficulty": "atomic | simple | moderate | complex | extreme | not_applicable",
  "issue_types": ["none"],
  "overall_score": 5
}
```

`issue_types` 支持：

```text
none
category_mismatch
ambiguous_intent
context_dependent
truncated
nonsense
emotional_only
too_brief
multi_intent
```

第二阶段保留四个输出字段之间的逻辑校验，包括：

- `issue_types` 必须非空、不得重复，且 `none` 不能与其他问题并存；
- 第一阶段判为 exact/self-contained/clear 时，不得输出与其直接矛盾的问题类型；
- 第一阶段 `valid` 时，两类 difficulty 不得为 `not_applicable`；
- `overall_score=5` 时必须且只能为 `issue_types=["none"]`；
- 第一阶段满格通过但总分低于 5 时，必须给出具体 issue。

第二阶段在 P1 补采完成、三个集合成员已经确定后，只对最终成员执行。其结果仅作为
标签，不参与 P0/P1/P2 的业务淘汰；但缺少合法第二阶段结果的记录不是完整成品，
禁止发布。

### 9.3 缓存与任务账本

run-local SQLite 缓存键包含：

- 阶段；
- Prompt SHA-256；
- 模型和公开参数哈希；
- `conversation_sha256`；
- L1/L2；
- 第二阶段还包含第一阶段结果。

因此，完全相同的 LLM 任务即使来自多个集合，也只调用一次；这只复用检测结果，
不会做跨集合成员去重。后续补采发现等价任务时，任务账本会合并新增的 source alias。

`stage1/2_tasks.jsonl`、`stage1/2_attempts.jsonl`、`stage1/2_results.jsonl` 和
`llm_result_cache.sqlite` 支持失败后续跑。任务及审计账本只保存哈希、类别、状态和
来源定位等脱敏信息，不保存 Query 原文。

## 10. P1 补采

P1 初始第一阶段结束后，按冻结 L2 配额计算当前缺口。每轮只从本次运行尚未处理的
候选中抽取，并先与已经准入的 P1 样本去重，再调用第一阶段。

默认补采参数：

```text
max_rounds = 3
default_pass_rate = 0.55
pass_rate_prior_strength = 10
minimum_pass_rate = 0.10
maximum_pass_rate = 0.95
safety_factor = 1.1
```

各 L2 每轮请求量为：

```text
ceil(当前缺口 / 估计通过率 × safety_factor)
```

通过率以默认值为先验，根据初始及已完成补采的实际结果做贝叶斯平滑，并限制在
`[0.10, 0.95]`。新一轮仍按 L2、cluster、九宫格重新分配请求量，但最终 L2 目标
配额不漂移。

新候选通过第一阶段后，如果超过对应 L2 的剩余配额，则按确定性采样顺序截断。
每一轮记录请求量、估计通过率、抽样量、去重后数量、第一阶段通过量和实际准入量。

补采在以下任一条件下停止：

- `quota_filled`：全部可分配 L2 配额已满足；
- `candidate_exhausted`：没有新的可用候选；
- `max_rounds`：完成三轮后仍有缺口。

P1 最终不足 10,000 时，记录实际数量、总缺口、未分配目标和逐 L2 缺口。业务缺口
允许发布，但不得通过跨 L2 挪用、复制样本或伪造记录补齐。

## 11. Screened canonical

当前 `screened_schema_version=1`。每条内部权威成品结构为：

```json
{
  "screened_schema_version": 1,
  "set": "P0 | P1 | P2",
  "candidate": {},
  "quality": {
    "category_fit": "exact",
    "semantic_completeness": "self_contained",
    "intent_clarity": "clear",
    "validity": "valid",
    "classification_difficulty": "easy",
    "task_difficulty": "simple",
    "issue_types": ["none"],
    "overall_score": 5
  },
  "selection": {
    "origin": "initial_wave | supplement_wave",
    "round_id": 0,
    "candidate_task_id": "...",
    "stage1_task_id": "...",
    "stage2_task_id": "..."
  }
}
```

校验器要求：

- candidate 重新通过 Block A 的完整 canonical 校验；
- quality 恰好包含两个阶段的八个字段且逻辑一致；
- 每条记录必须通过统一第一阶段准入条件；
- `initial_wave` 必须对应 `round_id=0`；补采必须对应大于 0 的轮次；
- 只有 P1 可以出现 `supplement_wave`；
- 三个 task ID 必须能关联冻结任务账本；
- 同一集合内 `source_identity_sha256` 唯一；
- 记录数必须与分支汇总、P1 冻结配额和缺口守恒。

每个分支的 `screened.canonical.jsonl` 是 v2 的内部事实源，不复制到下游发布目录。

## 12. Legacy labeled v4 导出

Legacy exporter 不调用 LLM、不重新采样、不修改成员，只做确定性映射。当前版本：

```text
legacy_schema = labeled_v4
legacy_mapping_version = 1
```

### 12.1 28 字段顺序

P0/P1 的第 15 个字段为 `relevance_p0`，P2 为 `relevance_p2`；其他字段完全一致：

| 序号 | 字段 | 来源/转换 |
| ---: | --- | --- |
| 1 | `text` | `context_turns + target_query` 以 ` | ` 拼接；单轮等于目标轮 |
| 2 | `raw_text` | `candidate.source.raw_text`，即 cluster 原始 `text` |
| 3 | `context_text` | `context_turns` 以 ` | ` 拼接 |
| 4 | `target_query` | `candidate.conversation.target_query` |
| 5 | `turn_count` | `context_turns` 数量加 1 |
| 6 | `sample_type` | 无上下文为 `single_turn`，否则为 `multi_turn` |
| 7 | `L1` | `candidate.taxonomy.l1` |
| 8 | `L2` | `candidate.taxonomy.l2` |
| 9 | `cluster_id` | `candidate.taxonomy.cluster_id` |
| 10 | `cluster_prob` | `candidate.signals.cluster_probability` |
| 11 | `prob_band` | `candidate.features.probability_band` |
| 12 | `frequency` | `candidate.signals.frequency` |
| 13 | `freq_band` | `candidate.features.frequency_band` |
| 14 | `norm_freq` | `normalized_frequency` 四舍五入至 4 位 |
| 15 | `relevance_p0` / `relevance_p2` | 对应 relevance 四舍五入至 4 位 |
| 16 | `length_bin` | `candidate.features.length_band` |
| 17 | `complexity` | `candidate.features.complexity` |
| 18 | `formality` | `candidate.features.formality` |
| 19 | `session_id` | `candidate.source.session_id` |
| 20 | `set` | 分支对应的 legacy 集合名称 |
| 21 | `quality_category_fit` | 第一阶段 `category_fit` |
| 22 | `quality_semantic_completeness` | 第一阶段 `semantic_completeness` |
| 23 | `quality_intent_clarity` | 第一阶段 `intent_clarity` |
| 24 | `quality_validity` | 第一阶段 `validity` |
| 25 | `quality_classification_difficulty` | 第二阶段同名字段 |
| 26 | `quality_task_difficulty` | 第二阶段同名字段 |
| 27 | `quality_issue_types` | 第二阶段同名字段 |
| 28 | `quality_overall_score` | 第二阶段同名字段 |

`set` 的固定映射为：

| canonical set | legacy set |
| --- | --- |
| `P0` | `P0_smoke` |
| `P1` | `P1_benchmark` |
| `P2` | `P2_adversarial` |

legacy 校验独立于映射函数，逐条验证字段名称、顺序、数量、类型、枚举、空值及与
canonical 的对应关系。所有字段都必须存在且不能为 `null`；只有 `context_text`
允许为空字符串。当前 labeled v4 要求 `cluster_id` 为整数，因此即使 canonical 和
taxonomy 支持统一的字符串 cluster ID，字符串 ID 也会在 legacy 导出阶段明确失败。

当前枚举可无损映射。未来新增 canonical 枚举而 labeled v4 无法表达时，导出必须
失败，不能静默降级、删除或写 `null`。

### 12.2 Provenance

每条 legacy 记录同时生成一条 `export_provenance.jsonl`，包含：

- 输出序号；
- source identity 和源行哈希；
- canonical 记录哈希；
- legacy 记录哈希；
- initial/supplement 来源与轮次；
- candidate、stage1、stage2 任务 ID；
- mapping version。

provenance 用于发现 canonical 或 legacy 文件被修改，以及定位每条交付记录的完整
任务来源。

## 13. v2 Run identity 与恢复

当前版本：

```text
pipeline_version = 2
manifest_schema_version = 1
p012_build_config_version = 1
```

run contract 包含：

- 源文件 SHA-256、stat 和 canonical cache ID；
- taxonomy SHA-256；
- canonical schema/index/feature rule 版本；
- P012 构建配置及版本；
- 两阶段并发、重试和等待配置；
- 两个 Prompt SHA-256；
- 模型及公开推理参数；
- screened canonical schema 版本；
- legacy schema 和 mapping 版本。
- run report schema 版本。

以上 contract 的稳定哈希形成 `config_id`。run 目录为：

```text
<output-root>/runs/<UTC timestamp>__<config-id 前 12 位>/
```

选择规则：

- 默认优先恢复同 contract 的 `created`、`running`、`incomplete` 或
  `publish_failed` run；
- 没有可恢复 run 时，复用同 contract 的 `completed` 或 `published` run；
- `--new-run` 强制创建新 run；
- `--run-id` 只接受与当前输入及完整 contract 完全相同的 v2 run；
- v1/旧 manifest、版本不同或 contract 不同均拒绝恢复。

manifest 记录各真实运行阶段、三个分支状态、错误类型、P1 补采轮次与缺口、产物
路径/哈希/行数，以及 invocation 次数、恢复次数、活动时长、墙钟时长和阶段时长。

## 14. 运行产物

```text
<output-root>/
├── _canonical_cache/
│   └── <cache-id>/
│       ├── canonical_candidate_index.sqlite
│       ├── canonical_metadata.json
│       └── canonical_input_audit.json
└── runs/
    └── <run-id>/
        ├── manifest.json
        ├── p012_run_contract.json
        ├── p012_build_summary.json
        ├── legacy_export_report.json
        ├── screening_run_report.json
        ├── screening_run_report.md
        ├── prompts/
        │   ├── stage1.md
        │   └── stage2.md
        ├── quality/
        │   ├── quality_run_contract.json
        │   ├── llm_result_cache.sqlite
        │   ├── stage1_prompt.md
        │   ├── stage2_prompt.md
        │   ├── stage1_tasks.jsonl
        │   ├── stage1_attempts.jsonl
        │   ├── stage1_results.jsonl
        │   ├── stage2_tasks.jsonl
        │   ├── stage2_attempts.jsonl
        │   └── stage2_results.jsonl
        ├── P0/
        ├── P1/
        └── P2/
```

每个分支目录包含：

- `candidate_tasks.jsonl`：初始或补采候选任务；
- `branch_audit.json`：采样、去重、通过、截断、准入和缺口统计；
- `screened.canonical.jsonl`：内部权威成品；
- 对应的 `*_labeled.jsonl`：28 字段兼容视图；
- `export_provenance.jsonl`：逐条哈希和任务来源；
- `legacy_export_report.json`：分支导出校验报告。

P1 额外包含 `supplement_summary.json`，记录冻结配额、每轮请求和结果、最终数量、
总缺口、未分配目标及逐 L2 缺口。

### 14.1 批跑报告

Run 根目录同时生成：

- `screening_run_report.json`：版本化、机器可读的指标事实源；
- `screening_run_report.md`：从同一 JSON 确定性渲染的汇报文档。

报告按 P0/P1/P2、L1、L2 和候选波次统计：Stage1 候选数、Query 基础可用率、
语义标签一致率、Stage1 准入率、最终数量及难度/质量分布。指标定义为：

```text
Query 基础可用率
= (self_contained + clear + valid) / Stage1 已完成候选

语义标签一致率
= 基础可用候选中的 category_fit=exact / 基础可用候选

Stage1 准入率
= (exact + self_contained + clear + valid) / Stage1 已完成候选
```

P1 另外报告冻结配额 L2 覆盖率、配额完成率、最终分布与目标配额分布偏差及逐 L2
缺口。P0/P2 的目标范围仅为实际进入 Stage1 的 L2，因为它们分别受结构采样和困难
候选门槛约束，不以覆盖全部 taxonomy L2 为目标。

语义标签一致率来自 Stage1 LLM，是自动一致性估计，不作为人工金标准确率。最终集
的 `category_fit=exact` 是准入约束，只用于契约校验，不作为独立汇报准确率。

## 15. 发布

下游只接收以下三个文件：

```text
P0-冒烟集/P0_smoke_set_v4_labeled.jsonl
P1-基准集/P1_benchmark_set_v4_labeled.jsonl
P2-对抗集/P2_adversarial_set_v4_labeled.jsonl
```

发布前必须满足：

- canonical index、源文件、taxonomy 和冻结 Prompt 未变化；
- SQLite 完整性校验通过；
- 第一阶段不存在未完成任务；
- 最终成员全部具有合法第二阶段结果；
- screened canonical 及集合内 identity 校验通过；
- candidate/quality 任务账本与每条 selection 关联一致；
- P1 冻结配额、最终计数和逐 L2 缺口守恒；
- 批跑报告与候选任务、Stage1 结果、最终 L2 分布和 Markdown 渲染一致；
- legacy schema、字段顺序、逐条映射和枚举校验通过；
- provenance 中的 source/canonical/legacy 哈希与实际文件一致；
- manifest 的产物路径、哈希和行数与磁盘一致。

P1 或 P2 的业务数量缺口本身不阻止发布；任何 LLM 工程失败、契约不完整、文件被
修改或账本不一致都会阻止发布。

发布必须显式提供 `--publish-root`。三个文件按一个事务处理：

1. 目标文件与运行产物字节完全相同时，视为幂等成功。
2. 任一同名文件内容不同，默认拒绝整组发布。
3. 只有显式指定 `--replace` 才允许替换不同内容。
4. 提交前在各目标目录写入并校验临时文件。
5. 替换时先保存原文件，三个文件全部提交成功后再删除备份。
6. 任一提交失败时，回滚本次已经提交的全部目标文件。

完整构建但未发布的 run 状态为 `completed`；只有三文件发布成功后才标记为
`published`。

## 16. 命令行入口

v2 唯一生产入口：

```powershell
python -m screening.run_pipeline `
  --source D:/data/cluster.jsonl `
  --taxonomy D:/data/taxonomy.json `
  --output-root D:/data/screening-runs `
  --no-publish
```

构建并发布：

```powershell
python -m screening.run_pipeline `
  --source D:/data/cluster.jsonl `
  --taxonomy D:/data/taxonomy.json `
  --output-root D:/data/screening-runs `
  --publish-root D:/data/delivery
```

主要参数：

| 参数 | 说明 |
| --- | --- |
| `--source` | cluster JSONL，必需 |
| `--taxonomy` | taxonomy JSON，必需 |
| `--output-root` | 索引和 run 工作区 |
| `--config` | P012 采样及补采配置 |
| `--stage1-prompt` / `--stage2-prompt` | 两阶段 Prompt |
| `--publish-root` | 下游交付根目录；正式发布时必需 |
| `--no-publish` | 只构建不发布，不能与 `--publish-root` 同用 |
| `--replace` | 允许事务替换不同内容的三份发布文件 |
| `--run-id` | 指定并恢复/复用契约完全一致的 v2 run |
| `--new-run` | 强制新建；不能与 `--run-id` 同用 |
| `--workers` | LLM 全局并发数，默认 2 |
| `--max-retries` | 每阶段最大尝试次数，默认 3 |
| `--sleep` | 请求节流间隔，默认 1 秒 |
| `--cache-root` | 覆盖 canonical cache 根目录 |
| `--verify-full-source-hash` | 复用索引时重新验证完整源 SHA-256 |

默认业务配置位于 `artifacts/config/pipeline.default.json`，默认 Prompt 位于：

```text
artifacts/prompts/quality_stage1.md
artifacts/prompts/quality_stage2.md
```

退出码：

| 退出码 | 含义 |
| ---: | --- |
| `0` | 构建/复用成功；若请求发布则发布成功 |
| `1` | 配置、输入、契约、构建验证或发布错误 |
| `2` | 存在可恢复的 LLM 工程失败 |

## 17. 默认业务配置

```json
{
  "config_version": 1,
  "sampling": {
    "p0": {
      "l1_big_threshold": 3000,
      "l2_per_l1_big": 3,
      "l2_per_l1_small": 2,
      "max_per_cluster": 5,
      "fallback_pool_size": 10,
      "mmr_lambda": 0.7,
      "dedup_threshold": 0.7
    },
    "p1": {
      "target_n": 10000,
      "probability_weights": {"high": 3, "mid": 2, "boundary": 1},
      "frequency_weights": {"hot": 2, "warm": 2, "cold": 1},
      "cold_boundary_floor": 1,
      "mmr_lambda": 0.7,
      "dedup_threshold": 0.75
    },
    "p2": {
      "target_n": 3000,
      "multi_turn_ratio": 0.4,
      "probability_cap": 0.7,
      "frequency_quantile": 0.5,
      "minimum_complexity": "moderate",
      "dedup_threshold": 0.75
    }
  },
  "p1_supplement": {
    "max_rounds": 3,
    "default_pass_rate": 0.55,
    "pass_rate_prior_strength": 10.0,
    "minimum_pass_rate": 0.1,
    "maximum_pass_rate": 0.95,
    "safety_factor": 1.1
  }
}
```

任何配置、Prompt、模型参数或版本变化都会改变 run identity，不能继续使用旧 run
中的冻结任务作为同一构建。

## 18. 模块与公共接口

| 模块 | 职责 | 主要接口 |
| --- | --- | --- |
| `candidate/contract.py` | 会话解析、identity、固定特征、candidate 契约 | `parse_conversation()`、`compute_fixed_features()`、`build_canonical_candidate()`、`validate_canonical_candidate()` |
| `candidate/index.py` | 流式扫描、SQLite 索引、恢复、定点读取 | `ensure_canonical_candidate_index()`、`CanonicalCandidateIndex.iter_by()`、`materialize()` |
| `candidate/sampling.py` | 三分支纯采样与去重 | `CandidateSampler.sample_p0()`、`sample_p1()`、`sample_p2()`、`deduplicate_branch_candidates()` |
| `quality/two_stage.py` | 两阶段输入/输出校验、并发、缓存和账本 | `run_stage1()`、`run_stage2()`、`stage1_passes()` |
| `pipeline/orchestrator.py` | P0/P1/P2 编排和 P1 补采 | `run_build()` |
| `delivery/screened.py` | 最终 canonical 构建与校验 | `build_screened_canonical_record()`、`validate_screened_canonical_records()` |
| `delivery/legacy.py` | 28 字段映射、复验和 provenance | `map_screened_to_legacy()`、`validate_legacy_record()`、`export_legacy_branch()` |
| `delivery/report.py` | 语义树、标签一致率和批跑汇报 | `build_run_report()`、`write_run_report()`、`validate_run_report()` |
| `pipeline/run_context.py` | run identity、manifest、恢复和计时 | `prepare_run_context()`、`update_manifest()` |
| `delivery/publish.py` | 产物复验和事务发布 | `collect_and_validate_artifacts()`、`publish_labeled()` |
| `pipeline/cli.py` | v2 总入口和退出码 | `execute_pipeline()`、`main()` |

默认入口只读取 cluster 和 taxonomy，不读取 labeled 数据作为候选输入，也不使用
动态补 `null` 的 schema 对齐方式。

## 19. 测试与验收要求

测试使用 `moto` Python 环境，覆盖以下层次：

- 会话解析：单轮、多轮、空轮、多空白、完整多模态/ASR 标记；
- 固定特征：概率边界、长度边界、frequency 并列/单值/缺失、relevance、复杂度和
  正式度黄金样例；
- 索引：合法、隔离、noise、重复、identity 冲突、taxonomy 冲突、中断恢复、缓存
  失效、定点读取、行哈希和源文件变化；
- P0：L2/cluster 选择、high 回退、MMR、每 cluster 上限和去重；
- P1：10,000 配额守恒、九宫格、冷门边界保底、冻结 L2 配额、三轮补采、过采截断、
  候选耗尽和确定性；
- P2：概率/频率/复杂度门槛、60/40 双轨、3,000 目标和不足记录；
- LLM：严格字段白名单、枚举、第二阶段字段矛盾、重试耗尽、断点恢复和跨集合缓存复用；
- 成品：第一阶段准入、第二阶段不改成员、P0/P2 无补采、P1 波次逐条追溯；
- Legacy：28 字段名称/顺序/类型/值、单多轮、四位小数、三个 set 值和无损枚举；
- 报告：L1/L2 分布、P1 配额完成率、基础可用率、标签一致率、波次守恒、脱敏及
  JSON/Markdown 一致性；
- 发布：缺失标签、文件篡改、provenance 哈希、不兼容 run、幂等发布、冲突拒绝、
  `--replace` 整组替换和失败回滚；
- 回归：`screening` 全量测试通过。

验收时必须同时满足：构建结果确定、任务账本可恢复、分支统计守恒、内部 canonical
与 legacy 逐条一致、任何工程不完整状态都无法发布。
