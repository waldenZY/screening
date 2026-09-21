# P0 Adaptive 改造方案 v0.2（Screening V3 讨论稿）

> 文档状态：方案讨论，尚未实施
> 当前 V3 生产 P0：仍使用从 V2 继承的按 L1/L2、最大 Cluster 结构采样，不补采
> 本文描述的是未来候选方案，不能作为当前运行配置或产物口径
> 更新日期：2026-09-21

## 1. 改造目标

当前 P0 的算法本身是自洽的，但它实际构建的是“本周 taxonomy 头部类别代表集”：

> 以“大 L2 + 最大 Cluster + 高置信样本”代理 Smoke 核心场景，导致每个 L2 100% 只来自单一 Cluster，覆盖的是“头部语义点”，不是“核心能力回归”。

因此是否需要改造取决于 P0 的产品定义：

| P0 定义 | 当前策略是否合适 |
| --- | --- |
| 每周从当期 taxonomy 中抽取头部、高置信代表样本 | 基本合适，只需补充多 Cluster、补采和诊断 |
| 固定覆盖一组必须回归的核心能力，并要求跨周可比较 | 不完全合适，因为当前由当周规模决定选哪些 L2，没有固定必选场景和最终配额 |

Anchor 方案只服务于第二种定义，不是所有 P0 都必须增加的组件。

新版目标调整为：

> **以稳定的 Smoke Capability Anchor 为核心单位，每周将其映射到当期动态 taxonomy，再从多个代表 Cluster 中抽取高置信、典型表达，并通过补采保证每个核心场景达到最低有效样本数。**

### 已确认的目录维护决议与映射建议

1. `clusters.jsonl` 与 taxonomy 均由上游 LLM 生成，不是稳定字典。
2. 头部类别通常较稳定，但 L2 名称、边界、拆分合并关系及长尾类别可能按周波动。
3. 因此不能把某一周的 L1/L2 名称或 cluster 标识写死为长期 P0 规则。
4. 已确认 Anchor 目录使用版本化 YAML/JSON，由维护者在固定周期更新，不在每次运行时自动改写。
5. 当前映射建议采用“**稳定 Anchor 目录 + 每次运行动态映射**”的两层设计，具体实现仍待讨论：
   - 固定的是 Anchor 的业务语义、稳定 ID、正反例边界、优先级和目标配额；
   - 不固定的是 Anchor 到当周 taxonomy、L2、cluster 的映射；
   - 映射失败时保留该 Anchor 及其缺口，不静默删除，也不默认把配额转移给其他 Anchor。

这里的“固定 Anchor”不是写死当周 taxonomy 标签，也不是把 Anchor 写死在代码中。Anchor 应放在有版本号的 YAML/JSON 配置中，经评审后变更；它是独立于上游生成标签的稳定业务能力定义。

---

## 2. 总体链路

```text
Smoke Capability Anchors
        ↓
映射到当周 taxonomy 的 L1/L2/Cluster
        ↓
每个 Anchor 分配固定 quota
        ↓
Anchor 内按多个代表 Cluster 分配名额
        ↓
Cluster 内偏向高置信、高频、中心表达
        ↓
通用 Stage1
        ↓
统计每个 Anchor 的有效样本缺口
        ↓
在对应 L2/Cluster 内增量补采
        ↓
直到 quota 满足 / 候选耗尽
        ↓
最终 P0
```

---

## 3. 核心变化一：P0 的配额单位从 L2 改成 Smoke Anchor

### 3.1 Anchor 到底是什么

Anchor 可以理解为“一个必须包含的 P0 场景及其稳定配额桶”，不是新的 taxonomy 层级。

- 如果一个核心场景长期稳定地一一对应某个 L2，可以直接用该 L2 路径作为配置键，不需要额外 Anchor 抽象；
- 如果同一个场景可能改名、移动到其他 L1、拆成多个 L2，或多个 L2 需要共享一份总配额，稳定 `anchor_id` 才有价值；
- Anchor 不负责自动发现核心场景，也不意味着必须对全部 L1/L2 做 LLM 判断。

所以在最终方案中，Anchor 是否保留，应根据历史 taxonomy 的实际改名、拆分和合并频率决定。

当前 taxonomy 每周动态变化，所以不能把：

```text
系统设置/设置闹钟
```

这种 L2 名称写死。

应该建立一层相对稳定的业务语义，例如：

```text
设置闹钟
拨打联系人
打开应用
导航地点
查询天气
播放音乐
调节音量
……
```

这些是由本链路维护、跨周稳定的 **Smoke Capability Anchor**。

每周再解决：

```text
Smoke Anchor
→ 本周哪些 L2 / Cluster 对应它
```

映射结果只在当前 run 内生效，并与本次 taxonomy SHA 一起冻结，不能被下一周直接当成固定映射复用。

所以：

```text
P0 quota key = Smoke Anchor
```

而不是：

```text
P0 quota key = 当周 L2
```


---

## 4. Smoke Anchor 目录怎么产生和维护

第一版采用：

> **历史数据生成初始候选 + 固定周期人工维护版本。**

Anchor 目录不是每周自动改写，也不要求每次运行人工确认。维护者可以按固定周期，例如每月、每季度或产品大版本，查看累计诊断后发布一个新的目录版本。

### 4.1 初始目录的数据来源

可以先分析前几周数据生成第一版 Anchor 和 quota 建议，但不能只看“最终 P0”，因为旧 P0 只覆盖头部 L2 和最大 Cluster，本身带有采样偏差。建议同时使用：

1. **历史 P0 初采、Stage1 和最终结果**：观察旧版实际选中了什么、通过率和数量波动；
2. **P0/P1/P2 的全部 Stage1 结果**：扩大每个语义区域的可用率与类别 exact 估计，尤其利用 P1 已经评估的大量候选；
3. **每周 taxonomy**：取得当周 L1/L2、population 和 L2 下的 cluster 列表；
4. **每周 cluster 源数据**：取得 `cluster_id`、文本、`cluster_probability`、`frequency`，并构造代表文本；
5. **现有核心档/次核心档**：只在初版生成和固定维护周期参与，不进入每周运行阻塞链路。

历史数据至少用于生成以下 Anchor 级统计：

```text
出现周数 / 总周数
每周候选数与保守可用量
每周 Stage1 基础可用率与 exact 率
每周可映射 L2 数、Cluster 数
单一 Cluster 最大占比
历史 P0 初采数、通过数、最终数
映射名称和表达方式的跨周波动
```

初始 Anchor 候选来自跨周重复出现的稳定语义能力。历史数据只能说明“常见、稳定、可获得”，不能单独证明“业务关键”；因此只需在首次建表和固定维护周期做一次业务取舍，不需要逐 run 人工确认。

你现在已经整理出的：

* 核心档；
* 次核心档；
* 非 Smoke 重点档；

可以直接作为 Anchor 候选来源。

由业务/测试侧确认：

```text
必须周周回归
重要但非核心
无需进入 Smoke
```

同时允许把：

> 流量不高但业务关键

的能力人工提级。

这一步不需要每周重新做。固定维护周期只处理：新增/下线能力、长期 unmapped、长期候选不足、配额长期过高或过低，以及明显的 Anchor 边界错误。

### 4.2 Anchor 配置文件字段

建议把稳定目录保存为独立的版本化 YAML/JSON，而不是写进 Python 代码。配置分为目录级元数据、全局默认规则和 Anchor 三层：

```yaml
schema_version: 1
catalog_version: "2026-09"
effective_from: "2026-09-01"
source_windows:
  - "20260824-20260830"
  - "20260831-20260906"
  - "20260907-20260912"

defaults:
  target_clusters: 3
  max_single_cluster_share: 0.5

anchors:
  - anchor_id: "smoke.alarm.create"
    name: "设置闹钟"
    definition: "用户明确要求新建一个闹钟"
    positive_examples:
      - "明早七点叫我起床"
      - "设置一个下午三点的闹钟"
    negative_examples:
      - "查看已有闹钟"
      - "删除明早的闹钟"
    retrieval_hints:
      aliases: ["新增闹钟", "创建闹钟", "定闹钟"]
      l1_terms: ["系统工具", "闹钟"]
    tier: "critical"
    target_quota: 8
    enabled: true
```

首版必需字段：

| 字段 | 用途 |
| --- | --- |
| `anchor_id` | 跨版本稳定主键，不能使用当周 L2 名称代替 |
| `name` | 人工可读名称 |
| `definition` | 明确能力范围，是自动映射的主要语义依据 |
| `positive_examples` | 告诉映射器哪些表达属于 Anchor |
| `negative_examples` | 区分相近能力边界，降低误映射 |
| `target_quota` | 最终 Stage1 通过样本目标数 |
| `enabled` | 保留历史记录的同时控制是否参与构建 |

`retrieval_hints`、`tier`、`target_clusters` 和 `max_single_cluster_share` 可以使用全局默认值或作为 Anchor 级覆盖。`retrieval_hints` 只是召回提示，不是固定 taxonomy 映射。

配置中不要保存长期有效的 `l2_key` 或 `cluster_id`。它们属于单次运行产生的映射产物。

### 4.3 quota 如何由历史数据给出建议

quota 表示“最终需要交付多少条有效 Smoke Case”，不等于历史流量占比。建议先由历史数据给出推荐值，再写入目录版本：

1. 根据跨周可映射 Cluster 数，估计一个 Anchor 至少需要覆盖多少种代表表达；
2. 每个代表 Cluster 分配少量 Case，并设置单 Cluster 占比上限；
3. 使用各周 Stage1 通过率和剩余候选量，估计这个配额是否能稳定完成；
4. 取跨周保守值，而不是取最好一周或平均值；
5. 总量超过 P0 时间预算时，先保证所有 critical Anchor 的最低覆盖，再分配剩余配额。

可以为每个 Anchor 同时计算两个值：

```text
覆盖 quota = 目标代表 Cluster 数 × 每 Cluster 最少 Case 数
稳定产能 = 前几周“可获得且预计能通过 Stage1”的保守值
```

如果稳定产能高于覆盖 quota，可以直接推荐覆盖 quota；如果稳定产能不足，不应自动把目标降到稳定产能，而应把该 Anchor 标为“配额可行性待确认”，在首次建表或固定维护周期决定是降低 quota，还是保留目标并接受可观测缺口。

这里的“保守值”、每 Cluster 最少 Case 数和单 Anchor 上限是少量全局策略参数，应通过历史回放一次校准；不需要为每个 Anchor 人工调整一套参数。最终配置仍显式保存 `target_quota`，保证同一目录版本下跨周目标不漂移。

---

## 5. 核心变化二：一个 Anchor 覆盖多个 Cluster

当前：

```text
一个 L2
→ 最大 Cluster
→ 5 条
```

新版改成：

```text
一个 Smoke Anchor
→ 一个或多个 L2
→ 多个主要 Cluster
→ 多种代表表达
```

第一版可以先设简单约束，例如：

```text
每个 Anchor 目标覆盖 3 个可靠 Cluster
单个 Cluster 不超过 Anchor quota 的 50%
```

具体数值后续实验再调。

`target_clusters` 是多样性目标，不是要求系统硬凑 Cluster 的准入门槛。当周只有 1–2 个可靠映射时，应使用已有可靠 Cluster并报告 diversity 缺口；不能为了达到 3 个而接纳 `partial` 或 `mismatch` 映射。`max_single_cluster_share` 在可靠 Cluster 不足时也只能作为告警指标。

这能直接解决当前：

> 136 个 L2 = 136 个 Cluster

的极端单 Cluster 问题。

---

## 6. Cluster 内部怎么选

P0 本身是 Smoke，所以不需要故意追求边界和困难。

Cluster 内仍然应该偏向：

```text
高 cluster_probability
较高 frequency
较高 relevance.p0
```

也就是：

> 优先典型、稳定、容易复现的真实表达。

同时保留现有去重 / 多样性机制，避免一个 Cluster 内全是近重复。

---

## 7. 每个 Anchor 配多少条

第一版不建议继续机械沿用：

```text
每 L2 = 5 条
```

因为新的单位已经变成 Anchor。

MVP 可以先从类似：

```text
每个 Anchor：8~10 条
目标覆盖 3 个 Cluster
```

开始。

为什么不是直接 20 条？

因为 P0 要的是快速 Smoke，不是 Coverage 主集；先保证：

> 核心场景 × 多表达模式

比单纯扩大样本量更重要。

---

## 8. Stage1 保持通用

P0 不需要单独修改 Stage1。

仍然判断：

```text
category_fit
semantic_completeness
intent_clarity
validity
```

Stage1 负责回答：

> 这是不是一个有效、可稳定执行的 Smoke Case？

P0 甚至应该是三个集合里对 Stage1 要求最严格、最没有争议的一个。

但 Stage1 只判断样本是否适配当周 taxonomy 类别，不能代替 Anchor 映射判断。新版需要同时保证：

```text
样本与当周 L2 exact
+
当周 L2/Cluster 与 Smoke Anchor exact
```

---

## 9. 增加 P0 补采

这是新版 P0 和当前实现的重要区别。

当前 P0 Stage1 淘掉以后就直接缩水。

新版：

```text
Anchor quota = 10

第一次抽 12
↓
Stage1 PASS 7
↓
deficit = 3
↓
继续从该 Anchor 的其他 Cluster / 未抽候选补
↓
直到10条或候选耗尽
```

request 数量可以参考 P1 的方法：

```text
request_n
≈ deficit / estimated_pass_rate × safety_factor
```

你目前已经看到不同场景 Stage1 PASS rate 差异很大：

* 查询天气 ~97%
* 设置闹钟 ~95%
* 打开应用 ~27%
* 搜索图片 ~18%

所以动态估算比固定“每次再抽 5 条”合理很多。

---

## 10. Cluster 配额建议

第一版不需要设计太复杂的 sqrt / 网格。

可以先采用简单规则：

```text
Anchor quota = Q
↓
按 Cluster population / relevance 排序
↓
选前 K 个代表 Cluster
↓
每 Cluster 设上限
↓
剩余名额按规模或稳定权重分配
```

例如：

```text
Q = 10

Cluster A：4
Cluster B：3
Cluster C：2
Cluster D：1
```

只是示意。

关键目标不是数学最优，而是：

> 不允许一个 Cluster 吃满整个 Anchor。

---

## 11. taxonomy / Cluster 动态映射

这是 P0 新方案里唯一比较特殊的一层。

建议新增一个：

```text
Smoke Anchor → 当前 taxonomy
```

映射步骤。

不能只根据 L2 名称做映射。建议采用“两阶段召回 + 真实样本校验”。

### 11.1 第一阶段：低成本召回映射候选

根据以下信息，为每个 Anchor 召回一批可能相关的 L2，再展开到 L2 下的 Cluster：

```text
Anchor 名称、描述、正例和反例
当周 L1/L2 名称与说明
Cluster 标签及代表文本
上一周期映射（只作为弱先验）
```

这一阶段追求较高召回率，可以容忍混入少量错误候选。L2 在这里主要是检索和报告维度，不作为最终可信映射单元；实际准入至少下沉到 Cluster，因为同一个动态 L2 下可能混入不同语义的 Cluster。

当前链路可直接使用的字段及作用如下：

| 数据 | 字段 | 在映射中的作用 |
| --- | --- | --- |
| Anchor 目录 | `name`、`definition`、正反例、aliases | 定义目标语义与边界 |
| taxonomy | L1、L2、`l2_key`、population、cluster 列表 | 低成本生成初步候选和容量信息 |
| canonical conversation | `target_query`、`full_text`、`sample_type` | 判断 Cluster 的真实语义 |
| signals/features | `cluster_probability`、`frequency`、`relevance.p0` | 挑选典型代表文本，不能直接判断 Anchor 语义 |
| 上一周期映射 | Anchor 与历史标签/表达的关系 | 仅作弱先验或缓存线索 |

当前链路没有可直接用于 Anchor 语义检索的 embedding 字段，因此只依靠现有数值信号无法完成新类别映射。首版建议：

1. 使用 aliases、关键词匹配和当周 L1/L2 名称做确定性召回；
2. 将未覆盖或可能相关的 taxonomy 名称分批交给 LLM 做语义召回；
3. 再以 Cluster 代表文本做准入校验；
4. 对相同输入哈希缓存结果。

这个过程只生成当次运行的 `anchor_mapping`，不会自动新增、删除或修改 Anchor 目录。以后如果链路具备稳定的 embedding 服务，可以用 embedding 替换或补充第 1、2 步，但第 3 步的真实文本证据仍应保留。

### 11.2 第二阶段：按真实样本校验映射

对召回到的 Cluster，抽取多条代表文本判断其是否真正支持 Anchor。代表文本优先选择：

```text
Cluster 内高 cluster_probability 样本
文本完整、意图清晰的样本
覆盖 Cluster 主要表达方式的多个样本
```

映射判断至少输出：

```json
{
  "anchor_id": "p0_call_make_call",
  "taxonomy_path": "...",
  "cluster_id": "...",
  "mapping_label": "exact",
  "support_exact": 8,
  "support_total": 10,
  "contradiction_count": 1,
  "evidence_sample_ids": ["...", "..."],
  "mapping_source": "auto"
}
```

`mapping_label` 建议使用 `exact / partial / mismatch`。自动准入门槛应由多条证据中的 `exact` 支持率、最小支持条数和反例数共同决定，不采用 LLM 自报的置信度。具体门槛需要用真实映射标注集校准，当前不预设没有依据的固定值。

当 Anchor 语义比 L2/Cluster 更窄时，即使存在相关性也应标成 `partial`。首版无人审核运行不使用 `partial` 映射，以免 Stage1 通过了当周 L2，却仍不属于目标 Smoke Anchor。

### 11.3 映射结果处理：运行时无人值守

```text
高置信 exact
→ 自动进入 Anchor 候选池

灰区或 partial
→ 当前 run 不用于 P0，写入待观察清单

mismatch
→ 拒绝

没有合格映射
→ Anchor 状态记为 unmapped，保留配额缺口

映射存在但候选不足
→ Anchor 状态记为 insufficient_candidates
```

每个 run 固化一份带 taxonomy SHA 的 `anchor_mapping`，使最终 P0 可以追溯到“哪个 Anchor 通过哪些当周 L2/Cluster 得到哪些样本”。

每次运行不设置人工审核阻塞点。灰区、`unmapped` 和 `insufficient_candidates` 只进入报告；维护者在固定周期汇总多周结果，必要时更新 Anchor 目录的定义、正反例、召回提示或 quota。

第一版运行时可以全自动：

```text
Anchor 名称 / 描述 / 正反例
↓
和当周 L2 名称、多个代表文本做语义召回
↓
生成候选映射
↓
按真实样本进行 exact / partial / mismatch 校验
↓
只接纳满足自动准入规则的 exact 映射
↓
其他结果写入诊断，不阻塞本次构建
```

例如：

```text
Anchor：设置闹钟

本周 taxonomy：
系统设置/设置闹钟
系统工具/新增闹钟
……
```

都可以被挂到同一个 Anchor。

首版不建议只设置一个门槛，从全量 L2 中自动挑选“本周 Anchor”。这种做法会让 P0 的测试目标随流量与标签波动而变化，跨周数量可能稳定，但测试含义不稳定。

### 11.4 单次运行映射产物

稳定的 Anchor 目录与当周映射必须分开保存。每个 run 产生类似：

```json
{
  "catalog_version": "2026-09",
  "catalog_sha256": "...",
  "taxonomy_sha256": "...",
  "run_id": "...",
  "anchors": [
    {
      "anchor_id": "smoke.alarm.create",
      "status": "mapped",
      "mapped_clusters": [
        {
          "l1": "...",
          "l2": "...",
          "l2_key": "...",
          "cluster_id": "...",
          "support_exact": 8,
          "support_total": 10,
          "contradiction_count": 1,
          "evidence_sample_ids": ["..."]
        }
      ]
    }
  ]
}
```

同一个 `catalog_sha256 + taxonomy_sha256 + cluster source SHA + mapping method version` 的映射结果应可缓存复用；正式构建读取已经冻结的映射产物，避免一次 run 内重复调用或前后结果变化。

---

## 12. P0 的新验收指标

以后 P0 不应该重点汇报：

> 覆盖了多少 L2。

更应该看：

| 指标                    | 含义                     |
| --------------------- | ---------------------- |
| Smoke Anchor Coverage | 核心场景中有有效 Case 的比例      |
| Anchor Mapping Coverage | 核心场景中存在合格当周映射的比例   |
| quota completion      | 每个 Anchor 是否达到目标样本数    |
| cluster diversity     | 每个 Anchor 覆盖多少 Cluster |
| max cluster share     | 是否又被单一 Cluster 垄断      |
| Stage1 PASS rate      | 核心场景候选质量               |
| final usable count    | 最终 Smoke Case 数        |
| unmapped anchors      | 因当周没有可靠映射而缺失的 Anchor 数 |
| insufficient anchors  | 有映射但有效候选不足的 Anchor 数    |

第一版最核心就是三个：

```text
Anchor覆盖率
配额完成率
每Anchor Cluster覆盖数
```

---

## 13. P0 和 P1 可以复用很多工程逻辑

P0 和 P1 都属于：

> **抽样前就知道目标桶是什么。**

所以两者实际上非常适合共享补采框架：

```text
quota
→ initial sample
→ Stage1
→ deficit
→ supplement
→ quota filled / exhausted / max rounds
```

区别只是：

```text
P0 bucket = Smoke Anchor
P1 bucket = L2
```

P0 的 Cluster 分配更偏：

> 中心、代表、稳定。

P1 的 Cluster 分配更偏：

> Coverage、多样性。

---

## 14. P0 v0.2 暂时不做什么

第一版建议明确不处理：

* 70%~80% 固定锚点、20%~30% 周滚动；
* 多周 P0 资产治理；
* 历史 Case 替换策略；
* 跨周稳定率；
* 完整的 Anchor 生命周期审批系统。

这些属于你前面说的：

> 长期治理和交付。

当前只解决：

> **这一周怎么从动态 taxonomy 和真实候选里，构建一版真正像 Smoke 的 P0。**

Anchor 的稳定 ID、目录版本和单次运行映射快照属于首版必需能力，不在上述排除范围内。

---

## 一句话总结 P0 v0.2

> **P0 从“按当周头部 L2、最大 Cluster 采样”调整为“以稳定 Smoke Capability Anchor 为配额单位，每周映射到动态 taxonomy，在多个代表 Cluster 中优先选择中心高置信表达，并依据 Stage1 通过情况对场景缺口增量补采”，使 P0 从头部样本集合转为真正的核心能力 Smoke 集。**


