# Stage1 独立复测与策略重筛

Python 使用 `moto`，从包含 screening/ 的项目根目录运行。离线验收不调用真实模型：

```powershell
conda activate moto
python -B -m unittest discover -s screening/module_tests/tests -p "test_*.py" -v
```

## 输入

支持 labeled JSONL 和 reference JSONL。两者均需 context_text、非空 target_query；reference 的 conversation_turns、sample_type 必须与会话一致。提供 text、turn_count、平铺 L1/L2 或嵌套 candidate_category 时校验其一致性。类别要同时提供非空 L1/L2，或同时为空。原始标签不进入模型。

无类别不是输入错误：只调用文本特征模型，category_review 为 not_provided。默认生产策略会因 category_not_provided 而不准入，但保留 direct/clarification/excluded 文本类型。不得把零准入误读成全部语义无效。原始行序、重复记录和来源字段保留。

## 真实模型复测

需要 `artifacts/config/llm_config.local.json`，或使用 --llm-config 指定现有配置；该文件被 Git 忽略。每条一次特征请求，有类别时另一次独立类别请求；无额外连通性请求、自动重试、去重或历史缓存，不调用 Stage2。生产 runner 的重试与缓存另按 quality 配置执行。

```powershell
python -m screening.module_tests.run_stage1 `
  --input "当前透传到外部的样本仅供参考/query_samples_all.jsonl" `
  --output-dir "screening/module_tests/outputs/reference_review"
```

可用 --prompt、--category-prompt、--policy、--limit、--llm-config。--policy 接收策略对象 JSON，不是完整 pipeline 配置。输出目录必须不存在或为空。

## 无模型重筛

直接读取成功复测的 results.jsonl，使用新策略生成结论，不加载模型配置、不发送请求：

```powershell
python -m screening.module_tests.run_stage1 `
  --replay-results "screening/module_tests/outputs/reference_review/results.jsonl" `
  --policy "D:/data/screening_policy.json" `
  --output-dir "screening/module_tests/outputs/reference_replay"
```

保留原始输入、特征、类别与模型观察来源，只替换策略结果。原记录缺少成功的独立观察时标工程错误，不能补造结果；输入与历史绑定不一致同样拒绝。

## 产物与状态

run.json 保存模式、输入与策略哈希、版本、计数和非敏感模型元数据；results.jsonl 保存来源、独立输入、原始响应、特征、类别、stage_status、observation_provenance、聚合结果和错误。report.md 为可读明细，results.xlsx 为可筛选表格；另有两份 Prompt 快照。重筛模式的 Prompt 元数据是工具当前模板，不表示再次执行；原模型来源以每条 observation_provenance 为准。

route 是文本类型，stage1_pass 含完整策略要求；工程失败时二者为 null。类别阶段失败仍保留成功特征。Excel 的1/0/空白分别表示准入/未准入/工程未判定；query及响应按文本保存，不执行公式。XLSX长单元格截短，完整内容以JSONL为准。

退出码：0全部完成（可含正常排除）；2有单条工程失败；1配置、目录或文件错误；130用户中断。无人工真值，不报告准确率。这里的文本筛选不认证正式用例已就绪，正式测试仍需构建环境、判据和证据。
