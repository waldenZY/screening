# Screening 当前交付字段字典（labeled v8）

核对日期：2026-10-29；labeled_mapping_version=9。P0/P1/P2 均使用同样的14个顶层字段及顺序；Stage2 是否启用不改变字段。旧 run 不迁移或续跑。

| 序号 | 字段 | 类型 | 含义 |
| ---: | --- | --- | --- |
| 1 | `text` | string | 单轮等于目标轮，多轮为前序和目标轮以 ` \| ` 连接。 |
| 2 | `context_text` | string | 前序轮次以 ` \| ` 连接；单轮为空字符串。 |
| 3 | `target_query` | string | 最后一轮用户输入。 |
| 4 | `turn_count` | integer | 会话轮数，至少1。 |
| 5 | `L1` | string | 一级语义类别。 |
| 6 | `L2` | string | 二级语义类别。 |
| 7 | `cluster_id` | integer | 源数据簇ID。 |
| 8 | `cluster_prob` | number | 簇概率，0～1，按候选索引规则处理缺失值。 |
| 9 | `frequency` | integer | 源频次，非负，按候选索引规则处理缺失值。 |
| 10 | `session_id` | string | 源会话ID。 |
| 11 | `set` | string enum | P0_smoke / P1_benchmark / P2_adversarial。 |
| 12 | `intent_label` | string enum | 唯一来自 Stage1：dialogue→qa，task→task；undetermined 不交付。 |
| 13 | `executable` | integer enum | 已准入、direct、单轮、目标轮超过2字符、冻结风险词未命中时为1，否则为0；0不删除样本。 |
| 14 | `stage1` | object | features、category、policy、screening，筛选结果可由冻结策略重建。 |

全部顶层字段必须存在且不得为 null；只有 context_text 可以为空字符串。会话字段必须一致，按内部记录顺序映射，不改变成员数。目标轮使用候选清洗值；模型输入从 conversation.full_text 恢复，不能仅根据导出目标轮重建实际模型输入。

## 类型与 Stage1

dialogue 包括信息、交流和仅在回复中完成的文字生成；task 包括实际操作、服务办理、文件或媒体的处理生成。有效操作在混合请求中优先。Stage1 features 严格为 intent_label、intent_reason、open_points、dependencies；依赖每项仅含 source、description。category 为独立核验结果，正式交付必须 exact。policy 为版本化完整快照，screening 为 route、admitted、reasons、policy_sha256；正式成员必须 admitted=true。

QA 有澄清点仍走 direct；task 有必要澄清走 clarification。风险匹配规则保持不变。可选附加标注不参与类型、成员或 executable 的计算，该标记不证明产品操作成功。

## 可选 Stage2 与审计

正式交付不再包含 quality_task_difficulty；不填 null、不填假难度，也不把未评估写为 not_applicable。显式启用 Stage2 后，结果写入各分支 optional_annotations/stage2.jsonl 和 stage2_report.json，并单独绑定模型任务及冻结哈希，三个数据集仍交付相同14字段。

内部 quality 仅包含 stage1 和映射后的 intent_label；selection、export_provenance 不再引用 Stage2 任务。当前 screened_schema_version=11、quality_contract_version=13、report_schema_version=13。具体环境实例、判据和执行证据由后续测试流程提供。
