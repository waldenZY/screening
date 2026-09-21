# Screening v2 配置使用手册

> 适用入口：`python -m screening.run_pipeline`  
> Pipeline 配置版本：`config_version=2`  
> 更新日期：2026-09-18

## 1. 文档用途

Screening v2 使用两份配置：

- `artifacts/config/pipeline.default.json`：输入、输出、采样、质量调用、索引和发布配置；
- `artifacts/config/llm_config.local.json`：LLM 地址、密钥、模型和请求参数。

配置好这两份文件后，可以直接一键运行：

```bash
python -m screening.run_pipeline
```

默认配置不发布文件。最终数据、canonical 成品和批跑报告保存在
`outputs.output_root/runs/<run-id>/`。

本项目使用严格 JSON Schema：字段不能遗漏、不能增加，JSON 中也不能写注释。

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
    "output_root": "../../outputs_v2"
  }
}
```

实际指向 `screening/outputs_v2`。Linux 和 Windows 都建议使用 `/` 作为 JSON 路径
分隔符。绝对路径直接使用，不会再拼接配置目录。

### 2.3 严格续跑条件

要恢复同一个未完成 run，必须保持所有影响数据或 LLM 任务的配置不变，并再次执行同一
命令。修改输入、Prompt、模型、质量运行参数或采样参数后，系统会创建新 run。

日志、索引性能和发布参数不改变数据 contract；但更换 `output_root` 会切换工作区，
因此系统无法在新目录中自动找到原 run。

## 3. 完整配置结构

当前默认结构如下：

```json
{
  "config_version": 2,
  "inputs": {},
  "outputs": {},
  "prompts": {},
  "quality": {},
  "index": {},
  "logging": {},
  "publication": {},
  "sampling": {
    "p0": {},
    "p1": {},
    "p2": {}
  },
  "p1_supplement": {}
}
```

以下章节逐项解释全部参数。

## 4. 基础运行参数

### 4.1 `config_version`

| 属性 | 内容 |
| --- | --- |
| 类型 | 整数 |
| 当前值 | `2` |
| 是否可自行调整 | 否 |
| 作用 | 标识 pipeline 配置 Schema |

必须保持为 `2`。它不是业务版本开关，修改后程序会拒绝配置。

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

### 4.4 `outputs.output_root`

| 属性 | 内容 |
| --- | --- |
| 类型 | 路径字符串 |
| 默认值 | `../../outputs_v2` |
| 作用 | canonical cache、run、最终产物和报告的工作区 |
| 是否改变数据 contract | 否 |

更换目录不会改变算法结果，但会切换缓存和 run 搜索位置。要续跑必须继续指向原来的
`output_root`。

### 4.5 `prompts.stage1` / `prompts.stage2`

| 属性 | Stage1 | Stage2 |
| --- | --- | --- |
| 默认值 | `../prompts/quality_stage1.md` | `../prompts/quality_stage2.md` |
| 作用 | 有效性、基础质量及类别检测 | 难度及综合质量标签 |
| 是否改变 run | Prompt 内容变化时是 | Prompt 内容变化时是 |

Prompt 文件必须恰好包含一个 `{CANDIDATE_JSON}`。修改空格或文字也会改变 Prompt 哈希，
从而建立新 run；不要在未完成 run 中途修改 Prompt。

## 5. `quality`：两阶段 LLM 执行参数

### 5.1 `quality.workers`

| 属性 | 内容 |
| --- | --- |
| 类型/范围 | 正整数，至少 `1` |
| 默认值 | `8` |
| 作用 | 每个质量阶段同时执行的 LLM 请求数 |
| 是否改变 run | 是，当前版本将它纳入质量运行 contract |

- 调大：提高吞吐，但更容易触发服务限流、超时和瞬时资源压力；
- 调小：更稳定，但总耗时增加。

建议从服务端明确支持的并发量出发设置。出现大量 HTTP 429、超时或连接错误时优先调小。

### 5.2 `quality.max_retries`

| 属性 | 内容 |
| --- | --- |
| 类型/范围 | 正整数，至少 `1` |
| 默认值 | `10` |
| 作用 | 每个阶段对失败任务的最大尝试轮数 |
| 是否改变 run | 是 |

成功任务会立即进入缓存，下一轮只重试失败任务。达到上限仍失败时程序返回退出码 `2`；
保持配置不变重新启动即可继续。

注意：不要为了续跑临时修改 `max_retries`，否则会生成新 run。

### 5.3 `quality.sleep_seconds`

| 属性 | 内容 |
| --- | --- |
| 类型/范围 | 大于等于 `0` 的数值 |
| 默认值 | `1.0` |
| 作用 | 每次实际 LLM 请求前的等待时间 |
| 是否改变 run | 是 |

调大可降低服务压力但会增加耗时；设为 `0` 吞吐最高，但只适合稳定且容量充足的本地服务。

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

发布是把 run 工作区中的三份 legacy labeled 文件复制到指定下游目录。它不上传网络，
也不删除 run 工作区中的文件。

### 8.1 `publication.enabled`

| 类型 | 默认值 | 作用 |
| --- | --- | --- |
| 布尔值 | `false` | 是否在构建和全量校验成功后自动发布 |

推荐保持 `false`，先人工检查批跑报告和成品，再决定是否复制或发布。

### 8.2 `publication.publish_root`

| 类型 | 默认值 | 作用 |
| --- | --- | --- |
| 路径字符串或 `null` | `null` | 三份下游文件的根目录 |

只有 `enabled=true` 时生效，此时不得为 `null`。发布参数不改变数据 run identity。

目标存在不同内容时默认拒绝。需要覆盖时只能在命令行显式增加 `--replace`。

## 9. `sampling.p0`：P0 冒烟集参数

P0 没有全局目标数量，只按 taxonomy 结构选取少量高置信度代表样本，不进行补采。

| 参数 | 默认值 | 含义与调整影响 |
| --- | ---: | --- |
| `l1_big_threshold` | `3000` | taxonomy 中 L1 population 达到该值时视为大 L1。调低会让更多 L1 使用“大 L1”规则。 |
| `l2_per_l1_big` | `3` | 每个大 L1 选择 population 最大的 L2 数量。调大增加覆盖和总量。 |
| `l2_per_l1_small` | `2` | 每个小 L1 选择的 L2 数量。调大增加覆盖和总量。 |
| `max_per_cluster` | `5` | 每个被选 cluster 最多保留多少条。调大增加 P0 总量，但可能增加重复。 |
| `fallback_pool_size` | `10` | 最大 cluster 没有 high probability 样本时，按 P0 relevance 取前多少条进入回退池。它不是最终必取数量。 |
| `mmr_lambda` | `0.7` | MMR 中 relevance 权重；范围 `[0,1]`。越大越偏高质量，越小越偏文本多样性。 |
| `dedup_threshold` | `0.7` | 字符二元组 Jaccard 大于该值视为重复。调低去重更严格，调高保留更多相似文本。 |

P0 每个 L2 只使用观测候选量最大的 cluster，因此它是冒烟集，不代表完整 taxonomy
覆盖。以上任一参数变化都会建立新 run。

## 10. `sampling.p1`：P1 基准集参数

P1 先按 taxonomy L2 population 的平方根分配配额，再在 L2 内按 cluster 规模平方根和
probability×frequency 九宫格抽样。只有 P1 会按 Stage1 通过后的 L2 缺口补采。

### 10.1 基础参数

| 参数 | 默认值 | 含义与调整影响 |
| --- | ---: | --- |
| `target_n` | `10000` | P1 最终数量目标。初采通过者按冻结 L2 配额限制；补采按剩余缺口逐批准入。候选不足时允许低于目标。 |
| `cold_boundary_floor` | `1` | 每个 cluster 的 boundary+cold 网格有候选时的最低配额。建议保持 `1`；过大会显著提高低置信度冷样本占比，甚至挤占其他网格。 |
| `mmr_lambda` | `0.7` | 网格内 MMR 的 P0 relevance 权重；越大越偏高概率/高频，越小越偏表面多样性。 |
| `dedup_threshold` | `0.75` | 集合内 Jaccard 去重阈值。调低更严格，通常会增加补采量。 |

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

## 11. `sampling.p2`：P2 困难有效集参数

P2 只执行一次初始采样，不补采。`target_n` 是进入 Stage1 前的采样目标，Stage1 淘汰
和去重后最终数量可以更少。

| 参数 | 默认值 | 含义与调整影响 |
| --- | ---: | --- |
| `target_n` | `3000` | 初始采样总目标，不是最终保证数量。 |
| `multi_turn_ratio` | `0.4` | 多轮轨道目标比例；`0.4` 表示约 1200 多轮、1800 单轮。范围 `[0,1]`。某轨道不足时不会自动转给另一轨道。 |
| `probability_cap` | `0.7` | 只允许 `cluster_probability < cap`。调低更困难但候选更少；调高候选更多但边界性减弱。 |
| `frequency_quantile` | `0.5` | 只选择 cluster 内低频侧的候选。范围 `[0,1]`；`0.5` 使用当前实现的四舍五入秩边界，表示中位附近，不是连续统计意义上的精确中位数。 |
| `minimum_complexity` | `moderate` | 固定规则复杂度最低要求，可选 `simple`、`moderate`、`complex`。规则复杂度是文本启发式，不是 Stage2 的任务难度。 |
| `dedup_threshold` | `0.75` | P2 集合内 Jaccard 去重阈值，优先保留 P2 relevance 更高的样本。 |

P2 仍需通过统一 Stage1 门槛，因此它准确地说是“低概率、低频、规则复杂且有效”的
困难集，不包含被 Stage1 判为类别错误、上下文缺失、模糊或无效的样本。

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

补采参数变化都会建立新 run。P0/P2 不读取这些参数，也不会产生补采任务。

## 13. 本地 LLM 配置

先复制模板：

```bash
cp artifacts/config/llm_config.example.json artifacts/config/llm_config.local.json
```

Windows PowerShell：

```powershell
Copy-Item artifacts/config/llm_config.example.json artifacts/config/llm_config.local.json
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

Stage1/Stage2 都只输出四个字段，通常不需要非常大的 `max_tokens`。具体下限应先用当前
服务试跑确认；调得过小可能截断 JSON，调得过大则增加异常长响应的空间。

当前客户端没有显式发送 temperature、top_p 或 seed，结果是否完全确定取决于服务端
默认值和模型实现。模型服务升级但名称不变时，run contract 无法识别服务端内部变化。

## 14. 哪些修改会创建新 run

| 修改项 | 是否改变数据 run identity | 说明 |
| --- | --- | --- |
| cluster 文件或路径 | 是 | source snapshot 包含文件状态和路径 |
| taxonomy 内容 | 是 | 使用文件 SHA-256 |
| Stage1/Stage2 Prompt 内容 | 是 | 使用内容 SHA-256 |
| P0/P1/P2 采样参数 | 是 | 会改变候选成员 |
| P1 补采参数 | 是 | 会改变请求量和成员 |
| `workers/max_retries/sleep_seconds` | 是 | 当前纳入 quality run contract |
| LLM endpoint、模型和公开请求参数 | 是 | endpoint 只记录哈希 |
| API Key | 否 | 密钥不进入 contract |
| index batch/cache/空间/验证参数 | 否 | 只影响索引运行方式 |
| 日志等级 | 否 | 只影响控制台输出 |
| 发布开关和目录 | 否 | 只影响是否复制交付文件 |
| `output_root` | contract 不变 | 但工作区改变后无法发现原 run |

如果只是修复 API Key、网络或服务状态，希望继续失败的 run，不要修改其他质量参数。

## 15. CLI 覆盖与一次性参数

查看完整参数：

```bash
python -m screening.run_pipeline --help
```

常用覆盖关系：

| CLI | 覆盖的配置 |
| --- | --- |
| `--source` | `inputs.source` |
| `--taxonomy` | `inputs.taxonomy` |
| `--output-root` | `outputs.output_root` |
| `--stage1-prompt` / `--stage2-prompt` | `prompts.stage1/stage2` |
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

## 16. 推荐使用方式

### 16.1 正式批跑

建议从仓库默认值开始，只填写：

```text
inputs.source
inputs.taxonomy
outputs.output_root
llm_config.local.json
```

保持 `publication.enabled=false`，先完成构建并检查：

```text
screening_run_report.md
P0/P1/P2 的 branch_audit.json
P1/supplement_summary.json
quality/stage2_consistency.jsonl
```

### 16.2 小规模试跑

为了先验证 LLM、Prompt 和输出格式，可以临时降低：

```json
"sampling": {
  "p1": {"target_n": 300},
  "p2": {"target_n": 100}
}
```

实际配置仍必须保留对应对象中的其他字段。降低目标只减少采样和 LLM 数量，不会缩短
首次完整 cluster 索引扫描；若要测试索引，需要另外准备小型 cluster fixture。

试跑配置和正式配置会生成不同 run，这是预期行为。

### 16.3 中断后续跑

保持输入、配置、Prompt 和 LLM 公开参数不变，重新执行：

```bash
python -m screening.run_pipeline
```

系统会恢复同 contract 的未完成 run，验证已完成检查点，并只执行尚未完成的阶段或
LLM 任务。退出码 `2` 表示存在可以继续重试的 LLM 工程失败。

### 16.4 Linux 后台运行

```bash
nohup python -u -m screening.run_pipeline >> screening.log 2>&1 &
```

查看进度：

```bash
tail -f screening.log
```

不要在同一 `output_root` 中同时启动两个相同配置的进程。

## 17. 常见误区

- P1 `target_n=10000` 是最终目标，但候选耗尽或三轮补采后仍可能不足。
- P2 `target_n=3000` 是初始采样目标，Stage1 后通常会小于 3000。
- P0 没有全局 target；调大 `max_per_cluster` 不等于固定 P0 总量。
- 去重阈值越低，去重越严格；不是越高越严格。
- `mmr_lambda` 越高越偏 relevance，越低越偏文本多样性。
- `probability_cap=0.7` 使用严格小于，不包含恰好 `0.7`。
- `minimum_complexity` 是固定文本规则，不是 Stage2 的 `task_difficulty`。
- Stage2 不参与准入；低分或跨阶段分歧不会自动删除样本。
- 发布只是复制经过复验的三份 legacy 文件，不是上传到远程系统。
- 改变 `workers` 或 `max_retries` 目前也会生成新 run；续跑时不要修改。

## 18. 配置检查清单

首次正式运行前确认：

- [ ] `inputs.source` 指向正确且已冻结的 cluster JSONL；
- [ ] `inputs.taxonomy` 的 cluster ID 类型、映射和 population 正确；
- [ ] `output_root` 与 `cache_root` 有足够空间；
- [ ] `llm_config.local.json` 已填写且未提交 Git；
- [ ] LLM 服务允许当前 `workers` 并发；
- [ ] Stage1/Stage2 Prompt 在批跑期间不会修改；
- [ ] P1/P2 目标和 P2 单多轮比例符合本次任务；
- [ ] 默认发布关闭，或已确认 `publish_root`；
- [ ] 同一配置没有另一个运行进程。

运行中断后确认：

- [ ] 没有修改影响 run identity 的参数；
- [ ] 使用原来的 `output_root`；
- [ ] 直接执行相同命令，不增加 `--new-run`；
- [ ] 日志中的 run ID 与原 run 一致。
