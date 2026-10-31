本文档记录 screening 模块输出产物（最终采样得到的 P0、P1、P2 数据集）的下游对接需求，为本项目参考材料。
文档持久保存在 docs 路径，不纳入版本迭代归档范围。文档内容发生修改时，视为下游需求变更，相关开发者需要评估并执行适配工作。
screening 项目的代码调整不会自动更新本文档；文档采用手动维护方式，保障下游需求无变更时，文档信息准确，避免误修改。
文档内容站在下游项目视角描述，涉及路径、脚本、文件等信息，请留意视角差异，确认对象指向。

---

# 具体信息

## 1. 路径结构

完整路径模式为：

```
$EVAL_DATA_ROOT/data/{周目录}/evaluation-set-large/
```

其中 `$EVAL_DATA_ROOT` 是环境变量，服务器上是 `/opt/huawei/data3/l00770403`。

### `{周目录}` 是怎么决定的？

在 `routes.py:62` 定义了正则：

```python
_WEEK_PATTERN = re.compile(r"^\d{6}-\d{4}-\d+days$")
```

即格式为 **`YYMMDD-MMDD-Ndays`**，例如：
- `260803-0809-7days`（表示 26年8月3日~8月9日，7天）

系统会扫描 `EVAL_DATA_ROOT/data/` 下所有匹配这个正则的目录，并且要求该目录下存在 `evaluation-set-large` 子目录，才算有效的周数据。最新的周目录会自动作为 `phone_eval_v4` 的默认数据源（`routes.py:131-137`）。

### 简单来说，tag 就是你数据的日期范围标签。

---

## 2. 三个数据集文件的目录和命名要求

`evaluation-set-large` 目录下需要包含三个子目录，每个子目录下放一个 JSONL 文件，结构和文件名是**硬编码**的：

```
evaluation-set-large/
├── P0-冒烟集/
│   └── P0_smoke_set_v4_labeled.jsonl
├── P1-基准集/
│   └── P1_benchmark_set_v4_labeled.jsonl
├── P2-对抗集/
│   └── P2_adversarial_set_v4_labeled.jsonl
```

这些路径定义在 `routes.py:92-96` 和 `config.yaml:30-32` 两处，**完全一致**：

| 别名 | 相对路径 |
|---|---|
| `P0_smoke` | `P0-冒烟集/P0_smoke_set_v4_labeled.jsonl` |
| `P0_smoke_raw` | `P0-冒烟集/P0_smoke_set_v4.jsonl`（可选，无 labeled 后缀的原始版） |
| `P1_benchmark` | `P1-基准集/P1_benchmark_set_v4_labeled.jsonl` |
| `P2_adversarial` | `P2-对抗集/P2_adversarial_set_v4_labeled.jsonl` |

**文件名中的 `v4` 和 `labeled` 后缀是代码硬编码要求的，不能随意改名。**

---

## 3. JSONL 数据字段要求

从 `_map_to_sample`（`offline_labels.py:360-419`）和 mock 数据模板，每条记录需要包含以下字段：

**核心文本字段：**
| 字段 | 说明 | 必需 |
|---|---|---|
| `text` | 查询文本 | 是 |
| `raw_text` | 原始文本（默认=text） | 否 |
| `context_text` | 上下文文本 | 否 |
| `target_query` | 目标查询（默认=text） | 否 |
| `turn_count` | 轮次数（默认1） | 否 |
| `sample_type` | `single_turn`/`multi_turn` | 否 |

**分类标签：**
| 字段 | 说明 | 必需 |
|---|---|---|
| `L1` | 一级标签 | 是（用于分类浏览） |
| `L2` | 二级标签 | 是 |
| `cluster_id` | 聚类ID | 否 |
| `intent_label` | `task`/`qa`（区分任务类/问答类） | workflow 过滤时必需 |
| `executable` | 0/1（1=风险操作，会被过滤） | 否 |

**信号/质量字段：**
| 字段 | 说明 |
|---|---|
| `cluster_prob` | 聚类概率 |
| `prob_band` | 概率分桶 |
| `frequency` | 频次 |
| `freq_band` | 频次分桶 |
| `norm_freq` | 归一化频次 |
| `complexity` | 复杂度 |
| `length_bin` | 长度分桶 |
| `formality` | 正式度 |
| `quality_overall_score` | 质量总分（1-5，workflow 默认>=5才通过） |
| `quality_category_fit` | 质量类别匹配 |
| `quality_validity` | 质量有效性 |
| `quality_intent_clarity` | 意图清晰度 |
| `quality_issue_types` | 质量问题类型（数组） |

**来源字段：**
| 字段 | 说明 |
|---|---|
| `session_id` | 会话ID |
| `relevance_p0` / `relevance_p2` | 相关性分数 |

---

## 4. 对你 p012 labeled 数据集的总结

你提供三个 labeled 数据集，需要放到：

```
$EVAL_DATA_ROOT/data/{YYMMDD-MMDD-Ndays}/evaluation-set-large/
    ├── P0-冒烟集/P0_smoke_set_v4_labeled.jsonl
    ├── P1-基准集/P1_benchmark_set_v4_labeled.jsonl
    └── P2-对抗集/P2_adversarial_set_v4_labeled.jsonl
```

- **周目录名**：按 `YYMMDD-MMDD-Ndays` 格式命名，如 `261020-1026-7days`
- **子目录名**：`P0-冒烟集`、`P1-基准集`、`P2-对抗集`（中文，代码硬编码）
- **文件名**：`P0_smoke_set_v4_labeled.jsonl`、`P1_benchmark_set_v4_labeled.jsonl`、`P2_adversarial_set_v4_labeled.jsonl`（固定，含 `_v4_labeled` 后缀）
- **数据内容**：每行一个 JSON 对象，至少需要 `text`、`L1`、`L2`、`intent_label`、`quality_overall_score`、`executable` 等字段
