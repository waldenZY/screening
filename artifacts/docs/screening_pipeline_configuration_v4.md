# Screening v4 配置使用手册

> 适用入口：`python -m screening.run_pipeline`
> Pipeline 配置版本：`config_version=8`
> 核对日期：2026-10-28；默认值以 [pipeline.default.json](../config/pipeline.default.json) 为准

## 1. 文档用途

Screening v4 使用两份配置：

- `artifacts/config/pipeline.default.json`：输入、输出、采样、质量调用、索引和发布配置；
- `artifacts/config/llm_config.local.json`：LLM 地址、密钥、模型和请求参数。

配置好这两份文件后，在 `screening` 包所在目录的上一级运行（Python 环境使用 `moto`）：

```bash
python -m screening.run_pipeline
```

默认配置不发布文件。最终数据、canonical 成品和批跑报告保存在
`outputs.output_root/runs/<run-id>/`。

本项目使用严格的配置字段契约：字段不能遗漏、不能增加，JSON 中也不能写注释。

## 2. 配置优先级与路径规则

### 2.1 参数优先级

同一个参数存在多种来源时，优先级为：

```text
CLI 显式参数 > pipeline 配置文件 > 程序默认值
```

CLI 只临时覆盖本次启动，不会回写 JSON 配置。

### 2.2 相对路径

配置文件里的所有相对路径，都相对于该配置文件所在目录解析，而不是相对于执行命令
时的当前目录。

默认配置位于：

```text
screening/artifacts/config/pipeline.default.json
```

例如：

```json
{
  "outputs": {
    "output_root": "../../outputs_v4"
  }
}
```

实际指向 `screening/outputs_v4`。Linux 和 Windows 都建议使用 `/` 作为 JSON 路径
分隔符。绝对路径直接使用，不会再拼接配置目录。

### 2.3 严格续跑条件

要恢复同一个未完成 run，必须保持所有影响数据或 LLM 任务的配置不变，并再次执行同一
命令。修改输入（包括风险关键词 Excel 内容）、Prompt、模型、质量运行参数或采样
参数后，默认会选择兼容 run 或创建新 run；显式指定不兼容的 `--run-id` 会报错。旧质量契约 run 不能按当前 Stage1/Stage2 规则续跑或发布。只更新默认值不升级契约版本；续跑之前的 run 仍须使用其原参数，不能直接套用已变化的新默认配置。

日志、索引性能和发布参数不改变数据 contract；但更换 `output_root` 会切换工作区，
因此系统无法在新目录中自动找到原 run。

自动选择优先恢复同契约的 `created/running/incomplete/partial_failed/publish_failed` run，再复用 `completed/published` run。总状态为 `failed` 的 run 不自动选中，必须显式使用 `--run-id`；恢复时仍会校验契约与检查点。

### 2.4 启动预检与故障定位

输入文件、风险词表、LLM 配置及依赖加载后，正式候选索引和批跑前执行启动预检，无需增加配置字段：

1. 四个 Prompt 均须恰好包含一个 `{CANDIDATE_JSON}`。
2. 对输出根目录、`runs/`、索引缓存目录，以及启用发布时的根目录和三个集合子目录，创建独立临时文件，验证写入、原子替换和删除，再清理探测文件。
3. 检查发布目标的文件类型和已有文件读权限。同名文件只提示，内容冲突在最终导出后比较哈希；相同内容仍允许重复发布，不同内容须 `--replace` 才能事务替换。
4. 按当前模型和请求参数发送一次固定短文本请求，验证能取得非空文本响应。预检不重试，失败退出码为 `1`，不启动索引或采样。

每次启动都包含这一请求，包括续跑或已完成 run 的复用；它不作为 Stage1、Challenge 或 Stage2 标注任务进入质量账本，不验证 Prompt 标注质量。成功摘要保存为 manifest 的 `preflight`，其中 `llm.requests=1`；该运行诊断不参与数据 contract，不要求新建 run。预检通过只表示当前条件可用，不保证后续服务状态、目录权限和文件锁不发生变化。

定位日志中的 `preflight` JSON 字段：

| check / 状态 | 关键字段与排查方向 |
| --- | --- |
| `prompt / failed` | `hint` 指出缺少或重复占位符的阶段 |
| `directory / failed` | `path`、`operation`、`error_type`、`errno` 指出创建、写入、替换或删除失败；检查权限、空间和文件锁 |
| `publication_targets / exists_unverified` | `set`、`path`、`replace`；已有文件保持原状，导出后比较哈希 |
| `llm / failed` | `error_type`、`http_status`、`hint`；401 检查密钥，403 检查授权，404 检查地址和模型，429 检查配额或限流 |
| `llm / passed` | `requests`、`elapsed_ms`、首 token 与完整响应耗时；地址仅记录 SHA-256 |
| `startup / passed` | 预检总耗时，随后才进入索引准备 |

预检日志及失败输出不包含 API Key、服务地址、服务响应正文或原始 Query。最终发布内容冲突使用 `publication` 日志，记录集合、目标路径及 `stop_without_replace` 或 `transactional_replace`，便于关联启动时的文件存在提示。

## 3. 配置结构与当前默认值

以下仅展示对象层级，空对象不是可运行配置。完整模板见 [默认配置](../config/pipeline.default.json)，参数字段必须全部保留。

```json
{
  "config_version": 8,
  "inputs": {},
  "outputs": {},
  "prompts": {},
  "quality": {},
  "screening_policy": {},
  "index": {},
  "logging": {},
  "publication": {},
  "sampling": {
    "p0": {},
    "p1": {},
    "p2": {},
    "dedup": {}
  },
  "p1_supplement": {},
  "p2_supplement": {}
}
```

当前默认：P1 目标 6,000，P2 目标 1,000；LLM 总并发 10，最大尝试轮数 20，请求前等待 1 秒；P2 总抽样预算 20,000、尾部预算 1,000、尾部单批 250、连续无进展上限 2。以下章节逐项解释全部参数。

## 4. 基础运行参数

### 4.1 `config_version`

| 属性 | 内容 |
| --- | --- |
| 类型 | 整数 |
| 当前值 | `8` |
| 是否可自行调整 | 否 |
| 作用 | 标识 pipeline 配置 Schema |

必须保持为 `8`。它不是产品大版本开关，也不同于内部构建契约版本；所有当前版本见 [文档索引](README.md)。旧配置版本或缺失必需字段会被拒绝。

### 4.2 `inputs.source`

| 属性 | 内容 |
| --- | --- |
| 类型 | 路径字符串 |
| 是否必填 | 正式运行必填；模板中允许暂时为空 |
| 作用 | cluster JSONL 数据源 |
| 是否改变 run | 是 |

每行必须为一个 UTF-8 JSON 对象。修改文件内容、路径、大小或修改时间，都可能生成新的
canonical cache 和 run。

示例：

```json
"source": "/data/screening/cluster.jsonl"
```

### 4.3 `inputs.taxonomy`

| 属性 | 内容 |
| --- | --- |
| 类型 | 路径字符串 |
| 是否必填 | 正式运行必填；模板中允许暂时为空 |
| 作用 | cluster 到 L1/L2 的唯一映射及声明 population |
| 是否改变 run | taxonomy 内容变化时是 |

P0 的结构选择、P1 的初始 L2 配额及所有输出标签都依赖 taxonomy。正式批跑前应确认
cluster ID 类型、L1/L2 映射和 population 已冻结。

### 4.4 `inputs.risk_keywords`

`inputs.risk_keywords` 为正式运行必填的 `.xlsx` 路径；模板中的空字符串须在启动前填写。配置文件和 CLI 覆盖值中的相对路径均以所选 pipeline 配置文件所在目录解析；CLI 可用 `--risk-keywords` 临时覆盖。使用绝对路径可避免歧义。

```json
"risk_keywords": "D:/data/risk_keywords.xlsx"
```

Excel 格式要求：

- 工作簿包含 `Sheet1`，首行严格按顺序为 `关键词`、`风险级别` 两列。
- 两列的词条值均为非空字符串，读取时去除首尾空白；风险级别不能使用数字。
- 至少有一条有效词条；完全空白的行跳过，额外非空列或不完整词条会报错。
- 风险级别没有固定枚举，例如 `高`、`中`、`低` 均可作为文本元数据；当前匹配不按级别分层。

格式示例（正式词条由项目维护）：

| 关键词 | 风险级别 |
| --- | --- |
| 定时开关机 | 高 |
| 显示重绘刷新区域 | 低 |

词表在 Stage2 之后用于确定性计算 `executable`。程序按 `shared/risk_keywords.py` 的前后缀规则和特殊映射提取核心词，同时保留原词，对最后一轮 `target_query` 作子串匹配。例如“定时开关机”生成“开关机”“关机”和原词，“请关机”即可命中。匹配不做语义推断、否定识别、词边界检查或大小写转换；维护词条时须考虑核心词过宽可能带来的命中。

任一风险级别的词条命中均令 `executable=0`，样本仍保留和导出。词表不参与 Stage1 准入或 Stage2 模型标注，也不改变 P0/P1/P2 成员。`executable=0` 还可能来自多轮、短目标轮或 Stage1 只适合澄清；未命中词表不保证该字段为 `1`，完整口径见 [字段字典](screening_labeled_v6_field_dictionary.md#executable-口径)。

文件缺失或格式错误在启动阶段报错。run 根目录的 `risk_keywords.json` 保存词条、核心词、规则版本、`source_sha256` 和 `rules_sha256`；这些哈希进入 manifest 的冻结契约。续跑时须保持词表内容一致，修改原 Excel 内容需要新 run。发布复验校验快照和哈希，并从冻结快照重算 `executable`。

### 4.5 `outputs.output_root`

| 属性 | 内容 |
| --- | --- |
| 类型 | 路径字符串 |
| 默认值 | `../../outputs_v4` |
| 作用 | canonical cache、run、最终产物和报告的工作区 |
| 是否改变数据 contract | 否 |

更换目录不会改变算法结果，但会切换缓存和 run 搜索位置。要续跑必须继续指向原来的
`output_root`。

默认 `index.cache_root=null` 时，工作区包含两类目录：

```text
<output_root>/
├── _canonical_cache/<cache-id>/   # 候选 SQLite 索引、输入审计、去重 IDF
└── runs/<run-id>/                 # 状态、冻结输入、质量账本、检查点、报告与导出
```

索引 ID 依据源文件路径、大小、修改时间、文件身份、taxonomy 哈希和索引/特征规则生成；同条件复用，条件变化新增缓存子目录。它不是 LLM 缓存。LLM 缓存在各 run 的 `quality/llm_result_cache.sqlite` 内，P0/P1/P2 共享同一 run 的缓存；新 run 不自动共享旧 run 的 LLM 结果。

同输入和兼容配置重跑会恢复或复用 run；更换数据、改变影响契约的参数或使用 `--new-run` 时创建新 run，旧目录保留。缓存和 run 都不自动清理。仍需恢复、复验或发布的 run，应保留其引用的索引缓存及冻结输入；删除 run 会丢失其检查点和标注缓存。不同 run 共用发布目录时仍使用固定文件名，可能发生内容冲突。

### 4.6 Prompts 与筛选策略

`prompts.stage1` 指向特征识别 Prompt，新增必填 `prompts.category` 指向独立类别 Prompt，默认 `../prompts/quality_stage1_category.md`。保留 challenge、stage2。CLI 可用 `--category-prompt` 临时覆盖；四份 Prompt 均需唯一 `{CANDIDATE_JSON}`，内容进入冻结契约。

顶层必填 `screening_policy`：

```json
{"version":1,"allowed_routes":["direct","clarification"],"require_category_exact":true,"runtime_capabilities":["public_information","system_app_state","current_screen_media","identified_device_resource"]}
```

只允许列出的路由与运行时能力，无重复项。生产必须 require_category_exact=true；独立参考重筛可以设为false，但不等于具备生产类别。修改策略或环境约定需新 run；无模型重筛见 [模块说明](../../module_tests/README.md)。模型请求不会接收策略。

## 5. `quality`：Stage1、Challenge、Stage2 执行参数

### 5.1 `quality.workers`

| 属性 | 内容 |
| --- | --- |
| 类型/范围 | 正整数，至少 `1` |
| 默认值 | `10` |
| 作用 | P0/P1/P2 共享的全局在途 LLM 请求上限 |
| 是否改变 run | 是，当前版本将它纳入质量运行 contract |

- 调大：提高吞吐，但更容易触发服务限流、超时和瞬时资源压力；
- 调小：更稳定，但总耗时增加。

三个分支的就绪请求按公平轮转取得请求位；某分支暂时没有任务时，其额度自动供其他分支使用。
建议从服务端明确支持的并发量出发设置。出现大量 HTTP 429、超时或连接错误时优先调小。

### 5.2 `quality.max_retries`

| 属性 | 内容 |
| --- | --- |
| 类型/范围 | 正整数，至少 `1` |
| 默认值 | `20` |
| 作用 | 每次阶段批次调用的最大尝试轮数，包含首次尝试 |
| 是否改变 run | 是 |

成功任务会立即进入缓存，下一轮只重试失败任务；它不是“首次调用加 20 次重试”。某分支达到上限仍失败时，其他分支继续；分支失败会使 CLI 返回 `2`。续跑保持参数不变，并显式指定原 `--run-id`，成功任务使用缓存。这不是跨续跑累计的总调用次数硬上限。

注意：不要为了续跑临时修改 `max_retries`，否则会生成新 run。

### 5.3 `quality.sleep_seconds`

| 属性 | 内容 |
| --- | --- |
| 类型/范围 | 大于等于 `0` 的数值 |
| 默认值 | `1.0` |
| 作用 | 每次 Stage1/Challenge/Stage2 实际 LLM 请求前的等待时间 |
| 是否改变 run | 是 |

调大可降低服务压力但会增加耗时；设为 `0` 吞吐最高，但只适合稳定且容量充足的本地服务。
此等待不适用于启动预检请求，也不是全局每秒请求数限制；多个工作线程可以同时等待。

## 6. `index`：大文件索引与缓存参数

这些参数主要影响首次索引的速度、内存、磁盘保护和验证强度，不改变 P0/P1/P2 成员。

### 6.1 `index.cache_root`

| 属性 | 内容 |
| --- | --- |
| 类型 | 路径字符串或 `null` |
| 默认值 | `null` |
| 作用 | canonical SQLite cache 的保存目录 |

为 `null` 时使用 `<output_root>/_canonical_cache`。多个 run 使用相同输入和规则时可复用
同一索引。建议放在空间充足、随机读写性能较好的本地磁盘。

### 6.2 `index.batch_size`

| 属性 | 内容 |
| --- | --- |
| 类型/范围 | 正整数 |
| 默认值 | `50000` |
| 作用 | 扫描多少行后提交一次 SQLite 并保存扫描进度 |

- 调大：提交次数更少，通常略快，但中断后可能重复处理更多行；
- 调小：检查点更频繁，写入开销更高。

它不控制 P1/P2 的采样批次，也不控制 LLM 请求数量。

### 6.3 `index.progress_every`

| 属性 | 内容 |
| --- | --- |
| 类型/范围 | 正整数 |
| 默认值 | `100000` |
| 作用 | 每处理多少行输出一次索引进度日志 |

只影响日志频率。调小便于观察，但会增加少量日志开销。

### 6.4 `index.sqlite_cache_mb`

| 属性 | 内容 |
| --- | --- |
| 类型/范围 | 正整数，单位 MiB |
| 默认值 | `1024` |
| 作用 | SQLite 页面缓存上限 |

调大通常能减少磁盘读取，但会提高内存使用。共享机器上应结合可用内存和其他进程设置；
它不是 Python 进程的总内存上限。

### 6.5 `index.min_free_space_gb`

| 属性 | 内容 |
| --- | --- |
| 类型/范围 | 大于等于 `0`，且不能小于 `reserve_free_space_gb` |
| 默认值 | `10.0` |
| 作用 | 开始新建或恢复索引前要求的最低剩余空间 |

空间不足时在写入前停止，避免索引构建挤满磁盘。

### 6.6 `index.reserve_free_space_gb`

| 属性 | 内容 |
| --- | --- |
| 类型/范围 | 大于等于 `0` |
| 默认值 | `8.0` |
| 作用 | 索引扫描过程中必须保留的磁盘余量 |

每次批量提交后检查。低于该值时中断构建；补足空间后可以恢复。

### 6.7 `index.verify_full_source_hash`

| 属性 | 内容 |
| --- | --- |
| 类型 | 布尔值 |
| 默认值 | `false` |
| 作用 | 复用索引时是否重新读取整个 cluster 文件并校验 SHA-256 |

- `false`：检查文件状态和头尾抽样指纹，启动快；
- `true`：完整读取源文件，验证更强，但数 GB 文件每次启动都会明显变慢。

一般运行推荐 `false`。归档验收、文件经过不可信复制或对完整性要求极高时可临时启用。

## 7. `logging`：日志参数

### 7.1 `logging.level`

允许值：

```text
CRITICAL
ERROR
WARNING
INFO
DEBUG
```

默认 `INFO`。正式批跑建议保持 `INFO`，可以看到索引、检查点恢复、LLM 缓存、补采、
导出和报告进度。该参数不改变 run identity。

## 8. `publication`：发布参数

发布是把 run 工作区中的三份 labeled labeled 文件复制到指定下游目录。它不上传网络，
也不删除 run 工作区中的文件。

### 8.1 `publication.enabled`

| 类型 | 默认值 | 作用 |
| --- | --- | --- |
| 布尔值 | `false` | 是否在各分支完成导出与分支校验后自动独立发布 |

`false` 时仍构建、导出和生成报告，只是不复制到下游目录。设为 `true` 后，P0/P1/P2 谁先完成谁先发布；全部成功后再进行全量复验和发布文件核对，不要求各分支同时完成。

### 8.2 `publication.publish_root`

| 类型 | 默认值 | 作用 |
| --- | --- | --- |
| 路径字符串或 `null` | `null` | 三份下游文件的根目录 |

只有 `enabled=true` 时生效，此时不得为 `null`。发布参数不改变数据 run identity。

目标存在不同内容时默认拒绝。需要覆盖时只能在命令行显式增加 `--replace`。

发布路径固定为：

```text
<publish_root>/P0-冒烟集/P0_smoke_set_v6_labeled.jsonl
<publish_root>/P1-基准集/P1_benchmark_set_v6_labeled.jsonl
<publish_root>/P2-对抗集/P2_adversarial_set_v6_labeled.jsonl
```

不存在则复制；同名同哈希则复用；同名不同内容则该分支发布失败，其他分支继续。发布使用临时文件、哈希核对、原子替换，并在捕获写入异常时尝试回滚本次事务。已成功发布的其他分支不回滚；最终全量复验失败也不撤销之前的独立发布文件。该机制不保证进程被强制终止或机器断电后的事务恢复。

manifest 按分支记录发布状态、目标路径、哈希及数量。全部构建、复验和发布成功时总状态为 `published`；部分分支失败时为 `partial_failed`、退出码 `2`；全部分支失败为 `failed`、退出码 `2`。启动预检、全量复验等整体错误返回 `1`，最终整组发布失败记为 `publish_failed`。运行时 `--no-publish` 可临时关闭发布，`--publish-root` 可启用并覆盖路径。

## 9. `sampling.p0`：P0 冒烟集参数

P0 没有全局目标数量，只按 taxonomy 结构选取少量高置信度代表样本。候选去重造成的 cluster 缺口会在 Stage1 前按原采样规则补位；候选耗尽时报告缺口。

| 参数 | 默认值 | 含义与调整影响 |
| --- | ---: | --- |
| `l1_big_threshold` | `3000` | taxonomy 中 L1 population 达到该值时视为大 L1。调低会让更多 L1 使用“大 L1”规则。 |
| `l2_per_l1_big` | `3` | 每个大 L1 选择 population 最大的 L2 数量。调大增加覆盖和总量。 |
| `l2_per_l1_small` | `2` | 每个小 L1 选择的 L2 数量。调大增加覆盖和总量。 |
| `max_per_cluster` | `5` | 每个被选 cluster 最多保留多少条。调大增加 P0 总量，但可能增加重复。 |
| `fallback_pool_size` | `10` | 最大 cluster 没有 high probability 样本时，按 P0 relevance 取前多少条进入回退池。它不是最终必取数量。 |
| `mmr_lambda` | `0.7` | MMR 中 relevance 权重；范围 `[0,1]`。越大越偏高质量，越小越偏文本多样性。 |

P0 每个 L2 只使用观测候选量最大的 cluster，因此它是冒烟集，不代表完整 taxonomy
覆盖。以上任一参数变化都会建立新 run。

## 10. `sampling.p1`：P1 基准集参数

P1 先按 taxonomy L2 population 的平方根分配配额，再在 L2 内按 cluster 规模平方根和
probability×frequency 九宫格抽样。P1 按 Stage1 通过后的 L2 缺口补采；P2 使用另一套
基于 Challenge 总量及类型缺口的补采算法。

### 10.1 基础参数

| 参数 | 默认值 | 含义与调整影响 |
| --- | ---: | --- |
| `target_n` | `6000` | P1 最终数量目标。初采通过者按冻结 L2 配额限制；补采按剩余缺口逐批准入。候选不足时允许低于目标。 |
| `cold_boundary_floor` | `1` | 每个 cluster 的 boundary+cold 网格有候选时的最低配额。建议保持 `1`；过大会显著提高低置信度冷样本占比，甚至挤占其他网格。 |
| `mmr_lambda` | `0.7` | 网格内 MMR 的 P0 relevance 权重；越大越偏高概率/高频，越小越偏表面多样性。 |

### 10.2 `probability_weights`

```json
"probability_weights": {
  "high": 3,
  "mid": 2,
  "boundary": 1
}
```

三个字段必须完整，值为非负整数，且不能全部为零。它们是相对权重，不是百分比。
提高某一档会提高该 probability band 在 cluster 配额中的倾向。

### 10.3 `frequency_weights`

```json
"frequency_weights": {
  "hot": 2,
  "warm": 2,
  "cold": 1
}
```

规则与 probability 权重相同。两个方向的权重相乘形成九宫格权重。最终数量还会受到
候选容量、取整、MMR、去重和后续补采影响，因此实际分布不保证严格等于权重比例。

P1 的平方根配额强调长尾覆盖，不等同于真实流量分布。以上任一参数变化都会建立新
run。

## 11. `sampling.p2`：P2 Adaptive 参数

P2 的 `target_n` 是最终 Challenge 样本目标。只有 Stage1 PASS 且 Challenge Judge
返回 `is_challenge=true` 的样本才能入集。

| 参数 | 默认值 | 含义与调整影响 |
| --- | ---: | --- |
| `target_n` | `1000` | 最终 P2 目标；候选不足时允许少于目标。 |
| `candidate_source_shares` | `0.3/0.3/0.2/0.2` | 初始轮 complex、multi_turn、boundary、exploration 请求份额；字段必须完整且总和为 1。 |
| `minimum_complexity` | `moderate` | complex 来源池最低规则复杂度。 |
| `boundary_probability_cap` | `0.5` | boundary 来源池使用严格小于该值。 |
| `soft_min_by_primary` | `50/50/50/50/50/20/5/1` | 按八类 `primary_challenge` 配置软下限；键必须完整，值为非负整数，总和不超过目标且单项不超过类型上限。 |
| `max_per_primary` | `350` | 单一 primary 的最终硬上限；必须满足 `8 × max_per_primary >= target_n`。 |
| `batch_size` | `1000` | 填充总量时每个 P2 小批最多抽取的候选数。 |

### `sampling.dedup`：三个集合共用的候选去重规则

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `normalization_rule` | `unicode_whitespace_and_category_P_removed_v1` | 固定文本规范化规则；当前版本只允许此值。 |
| `idf_rule` | `all_indexed_rows_unique_character_2_3grams_log((N+1)/(df+1))+1_v1` | 固定完整源索引文档频次与平滑 IDF 公式；当前版本只允许此值。 |
| `rules_sha256` | 默认配置中的规则哈希 | 对上述规则名称及版本的冻结哈希；当前版本只允许默认值。 |
| `candidate_jaccard_min` | `0.45` | 规范化字符二元组 Jaccard 达到此值才进入近似比较；仅用于召回文本对。 |
| `weighted_jaccard_min` | `0.80` | 完整源索引上字符 2/3-gram IDF 加权 Jaccard 自动剔除下限。 |
| `edit_similarity_min` | `0.90` | 规范化 Levenshtein 编辑相似度自动剔除下限。 |

先从同一套完整会话文本中移除 Unicode 空白与标点，不改写正文。规范化文本完全相同的候选直接去重，包含单字 query。近似剔除同时要求同一 L2、加权 Jaccard 和编辑相似度达标。保留原有代表样本优先级；临界保留对与被剔除对及分数写入各集合的 `dedup_audit.jsonl`。IDF 从完整 canonical 源索引计算一次并冻结，规则与内容哈希写入 run 契约。上述阈值是保守初值，需按真实 run 审计再校准；调整配置会启动新 run。

加权交集/并集使用 `math.fsum`。计算及审计复验仅对超出 `[0,1]` 不超过 `1e-12` 的浮点误差裁回边界；非有限、非数值或明显越界仍报错，确认阈值不增加容差。旧审计在内存副本上复验，不改写文件、检查点或哈希。错误包含集合、审计路径、物理行号和异常字段/数值，不包含 Query。

旧字段 `multi_turn_ratio`、`probability_cap`、`frequency_quantile` 和各集合的 `dedup_threshold` 已删除，配置出现这些
字段会直接报错。P2 的 `frequency` 不作硬过滤；P0/P1 仍按各自规则使用频次和相关度。

## 12. `p1_supplement`：P1 补采参数

P1 的 L2 最终目标在补采前冻结。每轮只从本次 run 尚未处理的 source identity 中取新
候选，并根据当前 L2 缺口和估计通过率决定计划请求量。轮内拆成小批，每批对某 L2
最多抽取当前缺口数；批后按 cluster 的 Stage1 通过情况调整下一批份额。配额填满或
源候选耗尽时停止该 L2，最多执行 `max_rounds` 轮。

| 参数 | 默认值 | 范围 | 含义与调整影响 |
| --- | ---: | --- | --- |
| `max_rounds` | `3` | 非负整数 | 最大补采轮数。`0` 表示关闭 P1 补采；调大提高补足机会并增加 LLM 成本。 |
| `default_pass_rate` | `0.55` | 位于最小/最大通过率之间 | 没有观测数据时使用的初始通过率。设得高会减少计划请求量，设得低会增加计划请求量。 |
| `pass_rate_prior_strength` | `10.0` | 大于 `0` | L2 通过率估计和 cluster 反馈的先验强度。越大越依赖先验，越小越快跟随实测通过率。 |
| `minimum_pass_rate` | `0.1` | `0 < min <= max <= 1` | 估计通过率下限，防止极低通过率导致单轮请求量无限膨胀。 |
| `maximum_pass_rate` | `0.95` | `0 < min <= max <= 1` | 估计通过率上限，防止过度乐观。 |
| `safety_factor` | `1.1` | 大于 `0` | 放大每轮计划请求量；可能增加 Stage1 调用，已填满配额的 L2 会在批间停止。 |

每轮大致请求量为：

```text
ceil(L2 当前缺口 / 估计通过率 × safety_factor)
```

计划请求量是轮内上限，不代表实际送检量。单批通过数不会超过提交该批时的 L2
缺口，因此补采通过者全部入集；初采仍可能因冻结配额产生通过但未入集的样本。

补采参数变化都会建立新 run。P0/P2 不读取这些 P1 参数。

## 13. `p2_supplement`：P2 自适应补采参数

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `max_rounds` | `3` | 初始轮之后最多执行的补采轮数。 |
| `default_yield` | `0.10` | 来源池端到端 Challenge 总产出率先验。 |
| `type_default_yield` | `0.0125` | 来源池到单一 primary 的产出率先验；不得高于 `default_yield`。 |
| `prior_strength` | `10.0` | 贝叶斯平滑的先验强度。 |
| `minimum_yield` | `0.005` | 产出率估计下限。 |
| `safety_factor` | `1.1` | 按缺口反推请求量时的安全系数。 |
| `exploration_share` | `0.20` | 补采请求固定保留给 exploration 的份额。 |
| `max_sampled` | `20000` | 初采和补采实际抽中候选的总上限，包括随后被去重或拒绝的候选；不是 LLM 调用数上限。 |
| `max_tail_sampled` | `1000` | 总量达到目标后，为追补类型软下限允许增加的候选上限。 |
| `tail_batch_size` | `250` | 尾部补采单批最大候选数。 |
| `max_stalled_batches` | `2` | 连续未缩小八类总缺口的尾部批次数上限；任一批缩小总缺口即归零。 |

每批完成后更新来源总产出率和来源到 primary 的产出率；总量已满但软下限未满足时进入
最多 250 条一批的尾部补采并重新选择最终成员。总抽样达到 20,000、尾部达到 1,000，
或连续两个尾部批次没有缩小类型总缺口时停止，允许带缺口交付。预算计数写入 P2 检查点，
续跑不会重置。
尾部候选同时计入总预算，不是额外增加 1,000 的总额度；达到目标的那一批仍按批前的 `target_fill` 模式计数，后续批次才进入 `coverage_tail`。全部配额满足、补采轮数上限或候选耗尽也会停止，具体 `next_action` / `stop_reason` 见 [P2 手册](screening_v4_p2_adaptive.md#6-自适应补采)。

每批日志以 `P2 batch_progress` 开头并附带 JSON，包含运行模式、计划与实际抽样数、
Stage1/Challenge 通过数、最终数量与目标缺口、八类数量与缺口、总预算、尾部预算、
连续无进展批数及 `next_action`。最终的 `P2 adaptive_complete` JSON 重复输出这些关键
状态和明确的 `stop_reason`。

## 14. 本地 LLM 配置

先复制模板：

```bash
cp screening/artifacts/config/llm_config.example.json screening/artifacts/config/llm_config.local.json
```

Windows PowerShell：

```powershell
Copy-Item screening/artifacts/config/llm_config.example.json screening/artifacts/config/llm_config.local.json
```

`llm_config.local.json` 包含密钥，不应提交 Git。

| 参数 | 类型 | 含义 | 是否改变 run |
| --- | --- | --- | --- |
| `api_url` | HTTP(S) URL | OpenAI-compatible chat completions 地址 | 是，但 manifest 只保存地址哈希 |
| `api_key` | 非空字符串 | 服务认证密钥 | 否，不写入 manifest |
| `model` | 非空字符串 | 模型名称或服务端模型路由名 | 是 |
| `max_tokens` | 正整数 | 单次响应允许的最大 token 数 | 是 |
| `timeout` | 正数，秒 | 单次 HTTP 请求超时 | 是 |
| `stream` | 布尔值 | 是否使用流式响应 | 是 |
| `enable_thinking` | 布尔值 | 传给服务端的思考开关 | 是 |
| `skip_special_tokens` | 布尔值 | 传给服务端的特殊 token 处理开关 | 是 |

Stage1、Challenge、Stage2 都输出短 JSON，通常不需要非常大的 `max_tokens`。具体下限应先用当前
服务试跑确认；调得过小可能截断 JSON，调得过大则增加异常长响应的空间。

当前客户端没有显式发送 temperature、top_p 或 seed，结果是否完全确定取决于服务端
默认值和模型实现。模型服务升级但名称不变时，run contract 无法识别服务端内部变化。

## 15. 哪些修改会创建新 run

| 修改项 | 是否改变数据 run identity | 说明 |
| --- | --- | --- |
| cluster 文件或路径 | 是 | source snapshot 包含文件状态和路径 |
| taxonomy 内容 | 是 | 使用文件 SHA-256 |
| 风险关键词 Excel 内容 | 是 | 使用文件和规范化规则 SHA-256；内容变化需新 run |
| Stage1/Challenge/Stage2 Prompt 内容 | 是 | 使用内容 SHA-256 |
| P0/P1/P2 采样参数 | 是 | 会改变候选成员 |
| P1 补采参数 | 是 | 会改变请求量和成员 |
| P2 补采参数 | 是 | 会改变请求路由和成员 |
| `workers/max_retries/sleep_seconds` | 是 | 当前纳入 quality run contract |
| LLM endpoint、模型和公开请求参数 | 是 | endpoint 只记录哈希 |
| API Key | 否 | 密钥不进入 contract |
| index batch/cache/空间/验证参数 | 否 | 只影响索引运行方式 |
| 日志等级 | 否 | 只影响控制台输出 |
| 发布开关和目录 | 否 | 只影响是否复制交付文件 |
| `output_root` | contract 不变 | 但工作区改变后无法发现原 run |

如果只是修复 API Key、网络或服务状态，希望继续失败的 run，不要修改其他质量参数。

## 16. CLI 覆盖与一次性参数

查看完整参数：

```bash
python -m screening.run_pipeline --help
```

常用覆盖关系：

| CLI | 覆盖的配置 |
| --- | --- |
| `--source` | `inputs.source` |
| `--taxonomy` | `inputs.taxonomy` |
| `--risk-keywords` | `inputs.risk_keywords` |
| `--output-root` | `outputs.output_root` |
| `--stage1-prompt` / `--challenge-prompt` / `--stage2-prompt` | `prompts.stage1/challenge/stage2` |
| `--workers` | `quality.workers` |
| `--max-retries` | `quality.max_retries` |
| `--sleep` | `quality.sleep_seconds` |
| `--cache-root` | `index.cache_root` |
| `--index-batch-size` | `index.batch_size` |
| `--index-progress-every` | `index.progress_every` |
| `--index-cache-mb` | `index.sqlite_cache_mb` |
| `--index-min-free-space-gb` | `index.min_free_space_gb` |
| `--index-reserve-free-space-gb` | `index.reserve_free_space_gb` |
| `--verify-full-source-hash` | 强制开启完整源哈希 |
| `--no-verify-full-source-hash` | 强制关闭完整源哈希 |
| `--log-level` | `logging.level` |
| `--publish-root` | 本次启用发布并覆盖发布目录 |
| `--no-publish` | 本次强制不发布 |

以下参数故意不写入长期配置：

- `--new-run`：忽略可恢复/复用 run，强制新建；
- `--run-id`：只恢复契约完全一致的指定 run；
- `--replace`：允许事务替换发布目录中的不同内容。

不要把 `--new-run` 写进日常启动脚本，否则每次重启都会创建新 run，无法续跑。

## 17. 推荐使用方式

### 17.1 正式批跑

建议从仓库默认值开始，只填写：

```text
inputs.source
inputs.taxonomy
inputs.risk_keywords
outputs.output_root
llm_config.local.json
```

保持 `publication.enabled=false`，先完成构建并检查：

```text
screening_run_report.md
P0/P1/P2 的 dedup_audit.jsonl
P0/P1/P2 的 branch_audit.json
P1/supplement_summary.json
P2/adaptive_summary.json
quality/stage2_results.jsonl
```

### 17.2 小规模试跑

为了先验证 LLM、Prompt 和输出格式，可以在完整配置对象中修改以下目标、类型下限和批次参数：

```json
"sampling": {
  "p1": {"target_n": 300},
  "p2": {
    "target_n": 100,
    "soft_min_by_primary": {
      "implicit_goal": 5, "multi_constraint": 5,
      "multi_turn_context": 5, "long_tail_expr": 5,
      "ambiguity": 5, "multi_intent": 2,
      "boundary": 1, "cross_app": 0
    },
    "batch_size": 100
  }
}
```

上面的片段不是可独立使用的完整配置；实际配置必须保留 P1/P2 对象中的其他字段。
P2 调整总目标时必须同时保证分类型软下限之和不超过 `target_n`、每项不超过
`max_per_primary`，并满足 `target_n <= 8 × max_per_primary`。
降低目标只减少采样和 LLM 数量，不会缩短
首次完整 cluster 索引扫描；若要测试索引，需要另外准备小型 cluster fixture。

试跑配置和正式配置会生成不同 run，这是预期行为。

### 17.3 中断后续跑

保持输入、配置、Prompt 和 LLM 公开参数不变，重新执行：

```bash
python -m screening.run_pipeline
```

系统会按第 2.3 节选择同 contract 的可续跑或已完成 run，验证已完成检查点，并只执行尚未完成的分支或 LLM 任务。`failed` 必须显式指定原 run；自定义配置也要继续使用原 `--config`：

```bash
python -m screening.run_pipeline --config /原路径/pipeline.json --run-id 原run-id
```

退出码 `0` 表示本次成功；`2` 表示分支构建或发布失败，不限于 LLM 错误；`1` 表示整体错误。重启并不自动解决配置不兼容、文件冲突或权限问题。已完成标注可复用，但每次启动仍会执行一次 LLM 连通性预检。

### 17.4 Linux 后台运行

```bash
nohup python -u -m screening.run_pipeline >> screening.log 2>&1 &
```

查看进度：

```bash
tail -f screening.log
```

不要在同一 `output_root` 中同时启动两个相同配置的进程。

## 18. 常见误区

- P1 `target_n=6000` 是最终目标，但候选耗尽或三轮补采后仍可能不足。
- P2 `target_n=1000` 是最终 Challenge 目标；候选不足、三轮结束或类型稀缺时可能不足。
- P0 没有全局 target；调大 `max_per_cluster` 不等于固定 P0 总量。
- 去重阈值越低，去重越严格；不是越高越严格。
- `mmr_lambda` 越高越偏 relevance，越低越偏文本多样性。
- `boundary_probability_cap=0.5` 使用严格小于，不包含恰好 `0.5`。
- `minimum_complexity` 是固定文本规则，不是 Stage2 的 `task_difficulty`。
- Stage2 不参与准入；低分或跨阶段分歧不会自动删除样本。
- 发布按分支复制经过分支复验的 labeled 文件；全部成功后另做全量复验，不上传远程系统。
- 改变 `workers` 或 `max_retries` 目前也会生成新 run；续跑时不要修改。

## 19. 配置检查清单

首次正式运行前确认：

- [ ] `inputs.source` 指向正确且已冻结的 cluster JSONL；
- [ ] `inputs.taxonomy` 的 cluster ID 类型、映射和 population 正确；
- [ ] `inputs.risk_keywords` 指向冻结的关键词 Excel；
- [ ] `output_root` 与 `cache_root` 有足够空间；
- [ ] `llm_config.local.json` 已填写且未提交 Git；
- [ ] LLM 服务允许当前 `workers` 并发；
- [ ] Stage1/Challenge/Stage2 Prompt 在批跑期间不会修改；
- [ ] P1/P2 目标、P2 类型上下限和来源份额符合本次任务；
- [ ] 默认发布关闭，或已确认 `publish_root`；
- [ ] 同一配置没有另一个运行进程。

运行中断后确认：

- [ ] 没有修改影响 run identity 的参数；
- [ ] 使用原来的 `output_root`；
- [ ] 直接执行相同命令，不增加 `--new-run`；
- [ ] 总状态为 `failed` 时显式指定原 `--run-id`；
- [ ] 日志中的 run ID 与原 run 一致。
