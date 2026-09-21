# Screening v3 技术方案

> 文档状态：当前实现基线（含 P1 逐批补采与 P2 Adaptive）
> 适用入口：`python -m screening.run_pipeline`
> 更新日期：2026-09-21

P2 的独立算法说明见 [P2 Adaptive 技术方案](screening_v3_p2_adaptive.md)；尚未实施的
P0 后续方案见 [P0 Adaptive 讨论稿](screening_v3_p0_adaptive_proposal.md)。

## 1. 目标与边界

Screening v3 从统一的 cluster JSONL 和 taxonomy JSON 出发，独立构建 P0、P1、P2
三套数据。系统先建立可恢复的 canonical 索引，再按三个集合各自的规则选择候选，
经过 Stage1、P2 Challenge Judge 和 Stage2，形成内部权威的 screened canonical，最后映射为下游依赖的
28 字段 labeled v4 文件。

完整链路如下：

```text
cluster JSONL + taxonomy JSON
→ 流式扫描、校验和 canonical 索引
→ P0 / P1 初始采样；P2 四来源池自适应采样
→ 第一阶段：有效性、基础质量及类别检测
→ 第一阶段判断准入条件；P1 初采再受冻结 L2 配额约束
→ P1 按冻结 L2 配额逐批补采，并依据 cluster 反馈调整份额
→ 仅 P2 的 Stage1 PASS 样本进入 Challenge Judge
→ P2 按总量、类型软下限和上限逐批补采与最终选择
→ 第二阶段：难度及综合质量标签
→ screened canonical
→ legacy labeled v4 确定性映射
→ JSON/Markdown 批跑报告
→ 全量一致性校验
→ 三文件事务发布
```

v3 的运行边界为：

- cluster 是全部候选样本的唯一数据来源；taxonomy 是 L1/L2 的唯一类别来源。
- P0、P1、P2 是三条独立链路。一个源样本可以同时被多个集合选中。
- 每个集合内部去重，不进行跨集合去重。
- 第一阶段是三个集合统一的业务准入门槛。
- Challenge Judge 只决定 P2 挑战样本资格和类型；第二阶段只补充标签，不改变集合成员。
- 第一、第二阶段的独立判断可以不一致；分歧作为质量信号记录，不作为工程失败。
- P1 和 P2 各自按独立算法补采；P0 始终只有初始波次。
- canonical screened 是内部事实源；legacy labeled 是当前下游交付格式。
- 批跑报告只聚合脱敏统计，不保存 Query 原文，也不增加 LLM 调用。
- v3 不读取旧 labeled 数据作为输入，也不恢复 v1/v2 run 或旧 manifest。

## 2. 设计原则

### 2.1 语义与兼容格式分离

内部数据使用语义清晰、版本化的嵌套 canonical 契约。旧字段名只在最终 exporter
中出现，避免 `relevance_p0`、`quality_*` 等兼容名称反向约束新链路的内部语义。

### 2.2 决策与标注分离

第一阶段决定三个集合的基础质量准入，Challenge Judge 再决定 P2 的挑战资格和类型；
第二阶段负责难度和综合质量描述。第二阶段的任意合法结果都不会淘汰成员。

### 2.3 定位信息与正文分离

索引保存候选筛选需要的统计、特征和原文件字节位置，不保存完整会话文本。候选被
某一分支选中后，系统才按字节位置定点读取源行并物化完整 canonical。

### 2.4 确定性与可恢复性

配额、排序、去重、哈希和导出均为确定性操作。LLM 结果使用 run-local 缓存和任务
账本；采样、Stage1、P1 逐批补采、P2 自适应批次、最终成员、Stage2、导出和报告均有原子检查点。
同一输入、配置、Prompt 和模型契约对应同一个 v3 run identity。

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
├── canonical_input_audit.json
└── canonical_database_validation.json
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
7. 新建索引使用 SQLite quick/integrity check 完整验证，并生成数据库验证侧车文件。

中断时通过 checkpoint 恢复扫描。恢复或复用前会确认源文件没有变化；可通过
`--verify-full-source-hash` 强制重新计算完整源 SHA-256。已完成索引再次打开时优先
核对源文件状态和抽样指纹、数据库文件指纹、Schema、元数据及验证侧车，不再默认
扫描整个 SQLite；只有首次升级、构建中恢复或指纹异常时才执行完整检查。

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

### 8.3 P2：自适应挑战集

P2 的最终目标是 2,000 条同时满足 Stage1 PASS 和 Challenge Judge
`is_challenge=true` 的样本。候选来自四个可重叠来源池：

- `complex`：`complexity >= moderate`；
- `multi_turn`：`sample_type=multi_turn`；
- `boundary`：`cluster_probability < 0.5`；
- `exploration`：不属于前三池的其余候选。

初始轮按 `30% / 30% / 20% / 20%` 分配请求量。每条候选记录完整
`source_tags`，实际产出率只归因到唯一的 `sampled_from`。池内先按 L2、再按
cluster 容量平方根分层，层内用稳定哈希排序；相同输入、配置和已评估集合会产生
相同候选计划。`frequency` 不参与硬过滤，只用于最终诊断。

初始轮后最多执行三轮补采。每批最多 2,000 条，批后立即更新来源池总产出率和
来源池到 primary challenge 的产出率。补采按当前总量缺口、八类软下限缺口和后验
产出率分配来源，同时固定保留 20% 给 exploration。停止条件为目标与软下限满足、
三轮完成、相关来源池耗尽或没有未评估候选。

最终选择先为八类 `primary_challenge` 各预留最多 100 条，再按剩余可用候选数的
平方根分配名额；单类最多 700 条。总量已满但某类不足 100 条时仍可继续定向补采，
新获得的稀缺类型会替换超过软下限的头部类型。三轮后仍不足时如实交付并报告
`target_gap`、逐类型缺口和候选耗尽证据。

## 9. 三阶段 LLM 质量处理

三个阶段都采用“一条样本一次请求”的调用方式，并使用同一组并发与重试配置。各阶段
按链路顺序执行，在阶段内部建立并发池。默认参数：

```text
workers = 8
max_retries = 10
sleep_seconds = 1.0
```

Prompt 分别冻结到 run 目录的 `prompts/stage1.md`、`prompts/challenge.md` 和
`prompts/stage2.md`。LLM 响应
只允许输出阶段白名单字段；多余字段、输入字段复写、非法枚举、缺字段或阶段内部
逻辑矛盾都会作为响应错误重试。两个阶段之间的判断差异不属于响应错误。

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

### 9.2 Challenge Judge：P2 挑战判定

Challenge Judge 仅处理 P2 的 Stage1 PASS 样本。输入仍只包含原始五字段，不提供
probability、frequency、候选来源或 Stage1 结果，避免启发式字段影响挑战判断。
输出严格限制为：

```json
{
  "is_challenge": true,
  "primary_challenge": "multi_turn_context",
  "challenge_types": ["multi_turn_context", "ambiguity"]
}
```

八类枚举为 `implicit_goal`、`multi_constraint`、`multi_turn_context`、
`long_tail_expr`、`ambiguity`、`multi_intent`、`boundary`、`cross_app`。
`is_challenge=false` 时必须输出 `primary_challenge=null` 和空列表；为 true 时类型列表
必须包含 1～3 个不重复枚举，primary 必须包含其中并排在第一位。其余类型按固定
枚举顺序排列。Judge 不输出 `challenge_level`。

primary 选择“去除后最能降低样本难度”的类型；无法区分时依次使用
`multi_turn_context`、`multi_intent`、`cross_app`、`multi_constraint`、`ambiguity`、
`implicit_goal`、`boundary`、`long_tail_expr`。Challenge 类型不得放宽 Stage1 的
exact、self-contained、clear、valid 准入条件。

### 9.3 第二阶段：难度及综合质量

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

第二阶段保留自身四个输出字段之间的逻辑校验，包括：

- `issue_types` 必须非空、不得重复，且 `none` 不能与其他问题并存；
- `overall_score=5` 时必须且只能为 `issue_types=["none"]`；
- 第一阶段满格通过但总分低于 5 时，必须给出具体 issue。

Stage2 可以在独立复核中标出 `category_mismatch`、`context_dependent`、`truncated`、
`ambiguous_intent` 等与 Stage1 不一致的信号，也可以把 difficulty 标为
`not_applicable`。系统不会要求模型强行服从 Stage1，而是生成稳定的分歧代码：

```text
category_fit
semantic_context
semantic_truncated
intent_ambiguous
intent_nonsense
intent_emotional_only
intent_too_brief
classification_not_applicable
task_not_applicable
```

分歧正常写入 Stage2 缓存和 legacy 的原质量字段，并另外写入脱敏的
`quality/stage2_consistency.jsonl`。该账本只含任务键、source identity 哈希、是否
一致及分歧代码，不含 Query 原文。

第二阶段在 P1/P2 补采完成、三个集合成员已经确定后，只对最终成员执行。其结果仅作为
标签，不参与 P0/P1/P2 的业务淘汰；但缺少合法第二阶段结果的记录不是完整成品，
禁止发布。

### 9.4 缓存与任务账本

run-local SQLite 缓存键包含：

- 阶段；
- Prompt SHA-256；
- 模型和公开参数哈希；
- `conversation_sha256`；
- L1/L2；
- 第二阶段还包含第一阶段结果。

因此，完全相同的 LLM 任务即使来自多个集合，也只调用一次；这只复用检测结果，
不会做跨集合成员去重。后续补采发现等价任务时，任务账本会合并新增的 source alias。

`stage1/challenge/stage2_tasks.jsonl`、对应的 attempts/cache_hits/results 账本和
`llm_result_cache.sqlite` 支持失败后续跑。任务及审计账本只保存哈希、类别、状态和
来源定位等脱敏信息，不保存 Query 原文。

## 10. P1 补采

P1 初始第一阶段结束后，按冻结 L2 配额计算当前缺口。每轮只从本次运行尚未处理的
候选中抽取，并先与已经准入的 P1 样本去重，再调用第一阶段。P0/P2 和 P1 初采规则
不受补采算法影响。

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

通过率以默认值为先验，根据初采和已完成补采的实际结果做贝叶斯平滑，并限制在
`[0.10, 0.95]`。每轮开始时固定该轮各 L2 的计划请求量及估计通过率；只在下一轮
重新估计。冻结 L2 目标配额和现有请求量公式不变。

每轮拆成多个小批。某 L2 在一个小批内的请求量为
`min(当前缺口, 本轮未使用请求量)`；仍需补采的 L2 合并提交 Stage1。每批返回后立即将通过样本计入对应
L2 配额，并更新 cluster 反馈。因为单批请求量不超过当前缺口，该批通过者都能入集；
初采时按冻结配额限制成员的规则仍保留。

小批的 cluster 份额同时考虑尚未抽取的源候选数和已观测的 Stage1 通过情况。对某
cluster `c`，令 `R_c` 为未抽过的源候选数、`E_c` 为已完成 Stage1 的候选数、`S_c`
为其中通过数，`p_L2` 为本轮固定的 L2 估计通过率：

```text
p_c = (S_c + pass_rate_prior_strength × p_L2) / (E_c + pass_rate_prior_strength)
w_c = sqrt(R_c) × p_c
```

把小批名额逐个给 `w_c / (本轮已分配给该 cluster 的名额 + 1)` 最大且仍有源候选的
cluster；同分按 cluster ID 排序。初采与此前补采的 Stage1 结果都参与 `E_c/S_c`；
仅被抽出但在送检前去重淘汰的样本不参与反馈。原有概率×频次网格、MMR 和集合内
去重仍在 cluster 内执行。若本轮计划请求量足以探索所有可用 cluster，保留每个
cluster 至少探索一次的规则，但在整轮内只计一次；若配额提前填满，则立即停采。
`cold_boundary_floor` 也按整轮累计。

某 L2 配额已满或没有未抽过的源候选时，停止该 L2；用完本轮计划请求量但仍有缺口，
则留到下一轮重新估计。低通过率本身不触发停止。每轮记录计划请求量、估计通过率、
实际抽样量、去重后数量、Stage1 通过量、入集量和逐批明细。

补采在以下任一条件下停止：

- `quota_filled`：全部可分配 L2 配额已满足；
- `candidate_exhausted`：有缺口的 L2 已没有未抽过的源候选；
- `max_rounds`：完成三轮后仍有缺口。

P1 最终不足 10,000 时，记录实际数量、总缺口、未分配目标和逐 L2 缺口。业务缺口
允许发布，但不得通过跨 L2 挪用、复制样本或伪造记录补齐。
`candidate_remaining_by_l2` 仅表示尚未抽取的源候选数，不表示其中仍有多少样本能
通过 Stage1。

## 11. P2 自适应补采与选择

P2 初始端到端产出率先验为 `0.10`，单个 Challenge 类型产出率先验为 `0.0125`，
先验强度为 `10`，估计下限为 `0.005`，安全系数为 `1.1`。每批完成 Stage1 和
Challenge Judge 后，系统用已观测请求数和命中数更新来源池总产出率及
`来源池 → primary_challenge` 产出率，再据此安排下一批。

每批处理顺序固定为候选计划、物化与全局去重、Stage1、Challenge Judge、合格池
更新和最终选择重算。候选计划与批结果均写入原子检查点；恢复时复用已完成计划、
Stage1 和 Challenge 结果，不重复调用模型。补采最多三轮，不增加连续零命中的硬
早停，也不设置总请求量硬上限；实际请求受每批 2,000 条、未评估候选和候选池耗尽
约束。

最终选择对每个 primary 内再次按 L2、cluster 分层，并使用稳定文本哈希打破并列。
Judge 通过但未入集的样本按 `type_cap`、`total_limit` 或 `diversity` 归因。报告同时
记录四来源漏斗、逐轮逐批产出率、八类分布与缺口、多标签组合、Judge 调用质量、
未入集原因、候选耗尽情况，以及 Challenge 与 Stage2 标签的交叉诊断。

## 12. Screened canonical

当前 `screened_schema_version=3`。下面以 P2 记录为例；P0/P1 的顶层
`challenge` 值为 `null`：

```json
{
  "screened_schema_version": 3,
  "set": "P2",
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
  "quality_consistency": {
    "is_consistent": true,
    "conflict_codes": []
  },
  "challenge": {
    "is_challenge": true,
    "primary_challenge": "multi_turn_context",
    "challenge_types": ["multi_turn_context", "ambiguity"],
    "challenge_task_id": "..."
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
- quality 恰好包含 Stage1 与 Stage2 的八个字段且各阶段内部逻辑成立；
- `quality_consistency` 必须与 Stage1/Stage2 重新计算出的分歧代码完全一致；
- 每条记录必须通过统一第一阶段准入条件；
- P2 的 `challenge` 必须包含合法且 `is_challenge=true` 的 Judge 结果及任务 ID；
  P0/P1 的顶层 `challenge` 必须为 `null`；
- `initial_wave` 必须对应 `round_id=0`；补采必须对应大于 0 的轮次；
- P1/P2 可以出现 `supplement_wave`，P0 只能是初始波次；
- candidate、stage1、stage2 及 P2 challenge task ID 必须能关联冻结任务账本；
- 同一集合内 `source_identity_sha256` 唯一；
- 记录数必须与分支汇总、P1 冻结配额和缺口守恒。

每个分支的 `screened.canonical.jsonl` 是 v3 的内部事实源，不复制到下游发布目录。

## 13. Legacy labeled v4 导出

Legacy exporter 不调用 LLM、不重新采样、不修改成员，只做确定性映射。当前版本：

```text
legacy_schema = labeled_v4
legacy_mapping_version = 1
```

### 13.1 28 字段顺序

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

### 13.2 Provenance

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

## 14. v3 Run identity 与恢复

当前版本：

```text
pipeline_version = 3
manifest_schema_version = 3
p012_build_config_version = 2
quality_contract_version = 3
checkpoint_schema_version = 1
screened_schema_version = 3
run_report_schema_version = 4
```

P2 Adaptive、Challenge Judge 及对应契约属于 Screening v3；其余内部 schema 版本按
各自兼容边界独立递增。

run contract 包含：

- 源文件 SHA-256、stat 和 canonical cache ID；
- taxonomy SHA-256；
- canonical schema/index/feature rule 版本；
- P012 构建配置及版本；
- P2 算法标识 `adaptive_challenge_batches_1`；
- 三阶段并发、重试和等待配置；
- 三个 Prompt SHA-256；
- 模型及公开推理参数；
- screened canonical schema 版本；
- legacy schema 和 mapping 版本。
- run report schema 版本。
- quality contract 与阶段检查点 schema 版本。

以上 contract 的稳定哈希形成 `config_id`。run 目录为：

```text
<output-root>/runs/<UTC timestamp>__<config-id 前 12 位>/
```

P012 构建契约也记录 P1 算法标识 `adaptive_cluster_batches_1` 和 P2 算法标识
`adaptive_challenge_batches_1`，用于验证构建检查点和续跑状态与当前算法一致。

选择规则：

- 默认优先恢复同 contract 的 `created`、`running`、`incomplete` 或
  `publish_failed` run；
- 没有可恢复 run 时，复用同 contract 的 `completed` 或 `published` run；
- `--new-run` 强制创建新 run；
- `--run-id` 只接受与当前输入及完整 contract 完全相同的 v3 run；
- v1/v2/旧 manifest、版本不同或 contract 不同均拒绝恢复。

manifest 记录各真实运行阶段、三个分支状态、错误类型、P1 补采轮次与缺口、产物
路径/哈希/行数，以及 invocation 次数、恢复次数、活动时长、墙钟时长和阶段时长。

### 14.1 阶段级检查点状态机

新 run 在 `<run-root>/checkpoints/` 原子写入以下检查点：

1. `initial_p0/p1`：P0/P1 完整 canonical 候选和确定性顺序；
2. `stage1_initial`：P0/P1 初始候选的 Stage1 结果；
3. `p1_quotas`：冻结的 L2 目标配额；
4. `p1_round_N_request`：第 N 轮各 L2 请求量、估计通过率和可用 cluster 数；
5. `p1_round_N_batch_B_plan/result`：第 N 轮第 B 批的候选计划、Stage1 结果和入集成员；
6. `p1_round_N_summary`：第 N 轮逐批汇总与停止原因；
7. `p2_round_N_batch_B_plan/result`：P2 候选计划、Stage1、Challenge 和选择结果；
8. `final_membership`：三个集合最终成员、来源波次、Stage1 和 P2 Challenge 结果；
9. `stage2`：最终成员的 Stage2 结果；
10. `screened_export`、`legacy_export`、`batch_report`：产物路径、大小和 SHA-256；
11. `build_outcome`：完整构建结果的最终检查点。

每个检查点都包含 schema 版本、run contract 哈希、记录数、payload 哈希和 payload。
临时文件不会被当作有效检查点。恢复规则为：

- manifest 未完成但检查点合法：承认检查点并回写完成状态；
- manifest 已完成但对应检查点缺失：停止，不能静默重算或复用错误数据；
- 检查点契约、内容哈希或产物哈希不一致：停止；
- 某分支没有完成：只重算该分支；
- P1/P2 轮次或小批已有计划：重启后复用计划和已完成的小批结果；未完成的
  Stage1/Challenge 任务由 run-local 缓存与任务账本继续处理；
- 已有 `final_membership`：跳过全部采样、Stage1 和补采；
- 已有 `stage2`：跳过质量调用，直接恢复导出或报告。

P1/P2 补采都可恢复到小批；不在单个小批内部建立检查点。构建契约包含两种自适应
算法标识，因此旧算法的 run 不会作为当前算法的同契约 run 自动复用。

### 14.2 旧 run 兼容边界

`pipeline_version=2` 或使用旧 manifest schema 的历史 run 不进入 V3 状态机，自动搜索
时忽略；显式指定旧 run ID 时返回不兼容提示。历史成品保留不变，输入不变时 canonical
索引仍可被新 run 复用。使用旧补采算法的 run 也因构建契约不同而不会被当前实现续跑。
完整 V2 代码由 Git `v2` 分支和提交 `b894753` 固定，文档、配置和 Prompt 快照见
[V2 归档](../archive/v2/README.md)。

对于升级部署时尚未完成、只被跨阶段分歧阻塞的历史 run，兼容性热修仅替换
`quality/two_stage.py`，保持原 Prompt、manifest 和 run identity，即可复用原 LLM
缓存完成该 run。历史 run 完成后再部署完整状态机；不能直接用完整新版恢复旧 run。

## 15. 运行产物

```text
<output-root>/
├── _canonical_cache/
│   └── <cache-id>/
│       ├── canonical_candidate_index.sqlite
│       ├── canonical_metadata.json
│       ├── canonical_input_audit.json
│       └── canonical_database_validation.json
└── runs/
    └── <run-id>/
        ├── manifest.json
        ├── checkpoints/
        ├── p012_run_contract.json
        ├── p012_build_summary.json
        ├── legacy_export_report.json
        ├── screening_run_report.json
        ├── screening_run_report.md
        ├── prompts/
        │   ├── stage1.md
        │   ├── challenge.md
        │   └── stage2.md
        ├── quality/
        │   ├── quality_run_contract.json
        │   ├── llm_result_cache.sqlite
        │   ├── stage1_prompt.md
        │   ├── challenge_prompt.md
        │   ├── stage2_prompt.md
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
        │   └── stage2_consistency.jsonl
        ├── P0/
        ├── P1/
        └── P2/
```

每个分支目录包含：

- `candidate_tasks.jsonl`：初始或补采候选任务；
- `branch_audit.json`：采样、去重、通过、准入和缺口统计；P1 初采另记录配额截断；
- `screened.canonical.jsonl`：内部权威成品；
- 对应的 `*_labeled.jsonl`：28 字段兼容视图；
- `export_provenance.jsonl`：逐条哈希和任务来源；
- `legacy_export_report.json`：分支导出校验报告。

P1 额外包含 `supplement_summary.json`，记录冻结配额、逐轮逐批的 L2/cluster 抽样与
送检结果、最终数量、未分配目标、逐 L2 缺口、源候选剩余量和停止原因。
P2 额外包含 `adaptive_summary.json`，记录四来源池容量与漏斗、逐轮逐批产出率、
类型配额和缺口、候选耗尽及停止原因。

### 15.1 批跑报告

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
缺口。P0 的目标范围仅为实际进入 Stage1 的 L2；P2 另按来源池和 Challenge 类型
统计覆盖，不以覆盖全部 taxonomy L2 为唯一目标。

报告 schema v4 保留 `p1_diagnostics`：逐轮计划请求、实际抽样、Stage1 评估、基础可用、
类别 exact、通过、入集与停止原因，以及缺口最大的 L2、文本不可用和类别不符损耗
最多的 cluster。`P1/supplement_summary.json` 保留完整逐批 L2/cluster 明细。
`p2_diagnostics` 新增四来源候选到 Stage1、Challenge 和最终入集的漏斗，逐批产出率，
八类 primary 数量、占比和软下限缺口，多标签组合，Judge 调用量、失败率、重试和
时延，Judge 通过但未入集原因，以及 Challenge 与 Stage2 的标签交叉诊断。

语义标签一致率来自 Stage1 LLM，是自动一致性估计，不作为人工金标准确率。最终集
的 `category_fit=exact` 是准入约束，只用于契约校验，不作为独立汇报准确率。

报告另按集合给出 Stage1/Stage2 分歧数量、分歧率及分歧类型分布。它用于说明两个
独立质量视角的一致性，不改变 Stage1 准入成员，也不等同于人工准确率。

## 16. 发布

下游只接收以下三个文件：

```text
P0-冒烟集/P0_smoke_set_v4_labeled.jsonl
P1-基准集/P1_benchmark_set_v4_labeled.jsonl
P2-对抗集/P2_adversarial_set_v4_labeled.jsonl
```

发布前必须满足：

- canonical index、源文件、taxonomy 和冻结 Prompt 未变化；
- canonical 索引的快速指纹、Schema 与元数据校验通过；首次升级、索引恢复或指纹
  异常时还必须通过完整 SQLite 检查；
- 第一阶段不存在未完成任务；
- P2 Challenge 不存在未完成任务，且所有最终 P2 都通过 Judge；
- 最终成员全部具有合法第二阶段结果；
- screened canonical 及集合内 identity 校验通过；
- candidate/quality 任务账本与每条 selection 关联一致；
- P1 冻结配额、最终计数和逐 L2 缺口守恒；
- P2 总量、单类上限、类型缺口、未入集原因和候选耗尽统计守恒；
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

## 17. 命令行入口

v3 唯一生产入口：

```powershell
python -m screening.run_pipeline
```

入口默认加载完整 `pipeline.default.json`。配置好输入和本地 LLM 文件后，无参数执行
即可构建；相同契约的未完成 run 自动恢复，已完成 run 直接复用。默认不发布。

CLI 可临时覆盖配置，例如构建并发布：

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
| `--source` | 覆盖配置中的 cluster JSONL |
| `--taxonomy` | 覆盖配置中的 taxonomy JSON |
| `--output-root` | 覆盖索引和 run 工作区 |
| `--config` | 选择完整运行配置 |
| `--stage1-prompt` / `--challenge-prompt` / `--stage2-prompt` | 三个质量阶段的 Prompt |
| `--publish-root` | 临时启用发布并覆盖下游交付根目录 |
| `--no-publish` | 临时关闭发布，不能与 `--publish-root` 同用 |
| `--replace` | 允许事务替换不同内容的三份发布文件 |
| `--run-id` | 指定并恢复/复用契约完全一致的 v3 run |
| `--new-run` | 强制新建；不能与 `--run-id` 同用 |
| `--workers` | 覆盖 LLM 全局并发数 |
| `--max-retries` | 覆盖每阶段最大尝试次数 |
| `--sleep` | 覆盖请求节流间隔 |
| `--cache-root` | 覆盖 canonical cache 根目录 |
| `--verify-full-source-hash` | 复用索引时重新验证完整源 SHA-256 |
| `--no-verify-full-source-hash` | 临时关闭配置中的完整源哈希复验 |
| `--log-level` | 覆盖控制台日志等级 |

配置文件内相对路径统一相对于配置文件目录解析。CLI 显式参数优先于配置值。
`--new-run`、`--run-id` 和 `--replace` 是一次性或高风险控制，只保留在 CLI。

默认完整配置位于 `artifacts/config/pipeline.default.json`，默认 Prompt 位于：

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

## 18. 默认完整运行配置

```json
{
  "config_version": 3,
  "inputs": {
    "source": "",
    "taxonomy": ""
  },
  "outputs": {
    "output_root": "../../outputs_v3"
  },
  "prompts": {
    "stage1": "../prompts/quality_stage1.md",
    "challenge": "../prompts/quality_challenge.md",
    "stage2": "../prompts/quality_stage2.md"
  },
  "quality": {
    "workers": 8,
    "max_retries": 10,
    "sleep_seconds": 1.0
  },
  "index": {
    "cache_root": null,
    "batch_size": 50000,
    "progress_every": 100000,
    "sqlite_cache_mb": 1024,
    "min_free_space_gb": 10.0,
    "reserve_free_space_gb": 8.0,
    "verify_full_source_hash": false
  },
  "logging": {
    "level": "INFO"
  },
  "publication": {
    "enabled": false,
    "publish_root": null
  },
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
      "target_n": 2000,
      "candidate_source_shares": {
        "complex": 0.3,
        "multi_turn": 0.3,
        "boundary": 0.2,
        "exploration": 0.2
      },
      "minimum_complexity": "moderate",
      "boundary_probability_cap": 0.5,
      "soft_min_per_type": 100,
      "max_per_primary": 700,
      "batch_size": 2000,
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
  },
  "p2_supplement": {
    "max_rounds": 3,
    "default_yield": 0.1,
    "type_default_yield": 0.0125,
    "prior_strength": 10.0,
    "minimum_yield": 0.005,
    "safety_factor": 1.1,
    "exploration_share": 0.2
  }
}
```

`inputs.source` 与 `inputs.taxonomy` 允许在模板中暂时留空，但执行前必须由配置或 CLI
提供。LLM 地址、密钥、模型及请求参数继续存放在不提交 Git 的
`artifacts/config/llm_config.local.json`。

业务采样、质量运行参数、Prompt、模型或输入变化会改变 run identity，不能继续使用
旧 run 中的冻结任务。日志、索引性能参数和发布设置不改变数据 identity，但其解析后
的有效值会写入 manifest 供审计。

## 19. 模块与公共接口

| 模块 | 职责 | 主要接口 |
| --- | --- | --- |
| `candidate/contract.py` | 会话解析、identity、固定特征、candidate 契约 | `parse_conversation()`、`compute_fixed_features()`、`build_canonical_candidate()`、`validate_canonical_candidate()` |
| `candidate/index.py` | 流式扫描、SQLite 索引、快速验证、恢复、定点读取 | `ensure_canonical_candidate_index()`、`validate_canonical_database()`、`CanonicalCandidateIndex.iter_by()`、`materialize()` |
| `candidate/sampling.py` | P0/P1 初始采样与通用去重 | `CandidateSampler.sample_p0()`、`sample_p1()`、`deduplicate_branch_candidates()` |
| `candidate/p2_adaptive.py` | P2 四来源池采样、后验产出率和最终配额选择 | `AdaptiveP2Sampler`、`select_final_p2()`、`source_yield_rates()` |
| `quality/two_stage.py` | Stage1/Challenge/Stage2 输入输出校验、并发、缓存和账本 | `run_stage1()`、`run_challenge()`、`run_stage2()`、`stage1_passes()`、`challenge_passes()` |
| `pipeline/orchestrator.py` | P0/P1/P2 编排和 P1/P2 自适应补采 | `run_build()` |
| `pipeline/checkpoints.py` | 原子阶段检查点、contract 与内容哈希验证 | `CheckpointStore` |
| `pipeline/config.py` | 完整配置 Schema、相对路径和 CLI 覆盖 | `load_pipeline_config()`、`apply_pipeline_overrides()` |
| `delivery/screened.py` | 最终 canonical 构建与校验 | `build_screened_canonical_record()`、`validate_screened_canonical_records()` |
| `delivery/legacy.py` | 28 字段映射、复验和 provenance | `map_screened_to_legacy()`、`validate_legacy_record()`、`export_legacy_branch()` |
| `delivery/report.py` | 语义树、标签一致率和批跑汇报 | `build_run_report()`、`write_run_report()`、`validate_run_report()` |
| `pipeline/run_context.py` | run identity、manifest、恢复和计时 | `prepare_run_context()`、`update_manifest()` |
| `delivery/publish.py` | 产物复验和事务发布 | `collect_and_validate_artifacts()`、`publish_labeled()` |
| `pipeline/cli.py` | v3 总入口和退出码 | `execute_pipeline()`、`main()` |

默认入口只读取 cluster 和 taxonomy，不读取 labeled 数据作为候选输入，也不使用
动态补 `null` 的 schema 对齐方式。

## 20. 测试与验收要求

自动化测试使用 `moto` Python 环境。当前仓库中的测试覆盖：

- P1 cluster 反馈份额、分批补采、中断恢复和报告数量守恒；
- Challenge 输出字段、枚举、顺序、空值、失败重试及缓存复用；
- P2 四来源边界、重叠去重、份额回填和确定性；
- 八类软下限、单类上限、稀缺类型替换和不足交付；
- 模拟 LLM 的三阶段端到端构建、P2 Challenge 准入、Stage2 最终成员限定、恢复及
  发布前完整复验。

发布校验还会在每次真实运行中复验 canonical、任务账本、P1 配额、P2 Challenge、
报告数量、legacy 映射、provenance 和 manifest 哈希。当前自动化测试没有替代真实
百万级输入上的性能、类型分布和 Judge 精度验证；正式验收仍需用固定输入完成一次
公司环境批跑并核对报告。
