# 独立模块测试

这里的脚本用于单独观察模块的真实输出，不运行完整数据构建链路。目前提供 Stage1 复测入口。

## 环境与模型配置

所有 Python 命令使用 `moto` 环境。下列命令从 `screening` 的上级目录运行，即同时包含 `screening/` 的项目目录：

```powershell
conda activate moto
python -m pip install -r screening/requirements.txt
```

复用项目现有模型配置。已有本地配置时直接使用；尚未配置时，复制模板后填写服务地址、API Key 和模型：

```powershell
Copy-Item screening/artifacts/config/llm_config.example.json `
  screening/artifacts/config/llm_config.local.json
```

配置字段与模板保持一致。该本地配置已被 Git 忽略，不会复制到结果目录；运行信息只记录模型参数及服务地址的哈希。

## 准备输入

从最终 P0/P1/P2 `*_labeled.jsonl` 中抽出需要检查的记录，每行保留一个 JSON 对象。也可以直接指定完整交付文件并用 `--limit` 控制条数。

不需要原始 cluster、taxonomy、canonical 或历史 run。脚本只校验下面六个必需字段，其余字段原样保存在结果中，不要求字段顺序：

| 字段 | 要求 |
| --- | --- |
| `text` | 完整交付文本，必须与恢复的各轮按 ` | ` 连接后的值一致。 |
| `context_text` | 已提供的上文，轮次以 ` | ` 分隔；单轮为空字符串。 |
| `target_query` | 最后一轮，非空字符串。 |
| `turn_count` | 与恢复后的轮次数一致的整数。 |
| `L1`、`L2` | 非空字符串，作为待核验类别。 |

文件使用 UTF-8，允许 UTF-8 BOM 和空行。空行跳过，其他行保留顺序和重复记录；错误行也会计入 `--limit`。

脚本从 `context_text + target_query` 恢复轮次，不再次清洗。实际送给模型的只有 `sample_type`、`conversation_turns`、`candidate_category`。原有 `quality_task_difficulty`、`intent_label`、`executable` 等标签不参与请求，也不作为人工真值。

交付文本可能已被清洗，所以这次测试是用当前 Prompt 重新判断交付样本，不保证逐字还原历史 Stage1 请求。

## 运行

输入文件和输出目录必须在运行时显式指定：

```powershell
python screening/module_tests/run_stage1.py `
  --input "D:\data\问题样本.jsonl" `
  --output-dir "D:\data\stage1_test_001"
```

缺少任一路径参数会提示用法，不调用模型。输出直接写入指定目录，不自动增加时间子目录：目录不存在时创建；已存在时必须为空，已有结果不会被覆盖。

| 参数 | 含义 |
| --- | --- |
| `--input` | 必填，待检查的 labeled JSONL。 |
| `--output-dir` | 必填，本次结果目录。 |
| `--prompt` | 可选，替换 Stage1 Prompt；默认使用当前 `artifacts/prompts/quality_stage1.md`。 |
| `--llm-config` | 可选，替换模型配置；默认使用现有 `artifacts/config/llm_config.local.json`。 |
| `--limit` | 可选，正整数，只处理前 N 个非空行，包含错误行和重复行。 |

用户传入的相对路径按当前工作目录解析；默认 Prompt 和模型配置按脚本所在项目解析。因此也可从其他目录使用脚本的绝对路径启动。

例如，只检查前 20 条，并指定另一份 Prompt：

```powershell
python screening/module_tests/run_stage1.py `
  --input "D:\data\问题样本.jsonl" `
  --output-dir "D:\data\stage1_test_002" `
  --prompt "D:\data\stage1_candidate.md" `
  --limit 20
```

每条有效输入顺序调用一次真实模型。没有额外连通性请求、自动重试、去重或历史缓存；无效输入不发请求。不调用 Stage2，不计算准确率。

## 查看结果

指定目录中包含五个文件：

| 文件 | 内容 |
| --- | --- |
| `results.jsonl` | 每个非空输入行对应一条结果，保留输入顺序。 |
| `results.xlsx` | 便于分享的可筛选明细表，包含原始字段、五项判断、准入结论、原始响应和错误。 |
| `report.md` | 可阅读的逐条模型输入、判断、原始响应和错误，以及数量汇总。 |
| `stage1_prompt.md` | 本次使用的 Prompt 快照。 |
| `run.json` | 运行状态、时间（UTC）、输入与 Prompt 哈希、模型参数、处理数量。 |

Prompt 按实际发送的文本保存，统一使用 LF 换行；`prompt_sha256` 对应该快照，`input_sha256` 对应原始输入文件字节。

可以直接把 `results.xlsx` 发给其他人：`Stage1结果` 工作表保留顺序和重复行，冻结表头与前四个标识列，并支持筛选。`stage1_pass` 中 `1` 表示通过、`0` 表示拒绝、空白表示失败未判定；失败状态为浅红色，拒绝结论为浅黄色。原始字段和新判断分列保存，原字段与结果列重名时增加 `source.` 前缀。

表格中的 Query、响应等按普通文本保存，不作为 Excel 公式执行。Excel 不支持的控制字符显示为转义文本；超出单元格 32,767 字符限制时会截短并标注，完整内容始终保留在 `results.jsonl` 中。超过 15 位的整数按文本保存，避免精度丢失。JSONL、Markdown、Prompt 快照和运行信息照常保留，生成表格不增加模型请求。XLSX 写入失败返回 `1`，已保存的 JSONL 等结果仍然保留。

`results.jsonl` 包含：

- `source_line`、`source_record`：输入文件物理行号和原始记录。非法 JSON 行的原文保存在 `source_text`。
- `stage1_input`：实际放入 Prompt 的三字段输入。
- `raw_response`：模型返回的原始文本。
- `stage1_result`：通过现有校验器的五项 Stage1 判断。
- `stage1_pass`：现有准入函数计算的 `true` 或 `false`；失败时为 `null`。
- `status`、`error`：成功或失败类型及错误；`elapsed_ms` 和两项延迟记录请求耗时。

状态说明：

| `status` | 含义 |
| --- | --- |
| `ok` | 模型输出格式和标签校验成功；准入仍可能是 `false`。 |
| `input_error` | 原始记录不合法、缺字段或会话字段不一致，没有调用模型。 |
| `request_error` | 请求或响应读取失败；无可取得的原始响应时为 `null`。 |
| `response_error` | 已取得原始响应，但不是合法 JSON 对象，或标签不符合 Stage1 契约。 |

单条失败后继续处理；失败不会计为拒绝，也不会推测一个替代判断。结果逐条落盘。Ctrl+C 后保存已完成结果和中断状态，未完成样本不计为失败；不支持在原目录续跑，需要换一个空目录。

退出码：`0` 表示所有选中样本均完成判断（包含正常拒绝），`2` 表示存在单条失败，`1` 表示配置、输入文件、Prompt、输出目录或文件写入错误，`130` 表示手动中断。

`module_tests/outputs/` 已被 Git 忽略，可手动指定它的某个空子目录存结果，但脚本不会默认写入这里。实际样本和结果可能包含原始内容，选择项目内其他目录时请自行避免提交。

## 离线验收

以下命令使用模拟模型，不发送真实请求：

```powershell
python -B -m unittest discover -s screening/module_tests/tests -p "test_*.py" -v
```

验收覆盖输入恢复、旧标签隔离、模型输出校验、部分失败、目录保护及独立启动。它验证脚本行为，不验证模型标注准确率。以后增加其他模块时，在本目录新增独立入口和相应说明。
