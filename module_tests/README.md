# 单模块复测：准入评估与下游质量标注

Python 使用 `moto`，从包含 screening/ 的项目根目录运行。离线验收不调用真实模型：

```powershell
conda activate moto
python -B -m unittest discover -s screening/module_tests/tests -p "test_*.py" -v
```

## 输入

准入工具支持 labeled JSONL、reference JSONL，以及新的 stage1.frozen.jsonl / screened.canonical.jsonl。文本记录需 context_text、非空 target_query；reference 的 conversation_turns、sample_type 必须与会话一致。提供 text、turn_count、平铺 L1/L2 或嵌套 candidate_category 时校验其一致性。类别要同时提供非空 L1/L2，或同时为空。原始标签不进入准入模型。

新交付文件同时有raw_text和stage1时，从raw_text恢复实际有序模型输入，并核对清洗后的目标轮与交付字段一致，保留生产模型看到的多模态标记；不会仅凭清洗后的target_query重建。内部候选同样按源原文恢复并核对。

无类别不是输入错误：只调用文本特征模型，category_review 为 not_provided。默认生产策略会因 category_not_provided 而不准入，但保留 direct/clarification/excluded 文本类型。不得把零准入误读成全部语义无效。原始行序、重复记录和来源字段保留。

## 真实模型复测

需要 `artifacts/config/llm_config.local.json`，或使用 --llm-config 指定现有配置；该文件被 Git 忽略。每条一次特征请求，有类别时另一次独立类别请求；无额外连通性请求、自动重试、去重或历史缓存，不调用 Stage2 的下游质量标注。这个工具只复测准入功能，不是单独运行生产 Stage1 的入口。生产 runner 的重试与缓存另按 quality 配置执行。

```powershell
python -m screening.module_tests.run_admission `
  --input "当前透传到外部的样本仅供参考/query_samples_all.jsonl" `
  --output-dir "screening/module_tests/outputs/reference_review"
```

可用 --prompt、--category-prompt、--policy、--limit、--llm-config。--policy 接收策略对象 JSON，不是完整 pipeline 配置。输出目录必须不存在或为空。

## 无模型重筛

直接读取成功复测的 results.jsonl，使用新策略生成结论，不加载模型配置、不发送请求：

```powershell
python -m screening.module_tests.run_admission `
  --replay-results "screening/module_tests/outputs/reference_review/results.jsonl" `
  --policy "D:/data/screening_policy.json" `
  --output-dir "screening/module_tests/outputs/reference_replay"
```

保留原始输入、特征、类别与模型观察来源，只替换策略结果。原记录缺少成功的独立观察时标工程错误，不能补造结果；输入与历史绑定不一致同样拒绝。

## 下游质量标注独立复测

新增入口只调用新的 quality_annotation Prompt，输出既有四个quality字段，不重新采样、不调用准入模型、不改写成员或executable。输入可为：

- 准入工具产生的 results.jsonl：要求对应行成功、具备真实L1/L2和类别核验，校验原始输入、独立判断和哈希绑定。
- 生产的 stage1.frozen.jsonl、screened.canonical.jsonl。
- 包含 stage1.admission 的新版下游 *_v4_labeled.jsonl。

~~~powershell
python -m screening.module_tests.run_quality_annotation `
  --input "screening/module_tests/outputs/reference_review/results.jsonl" `
  --output-dir "screening/module_tests/outputs/quality_review"
~~~

也可直接将 --input 指向某个生产run的 P1/stage1.frozen.jsonl。可用 --prompt、--llm-config、--limit；输出目录必须不存在或为空。每条一次请求，无额外连通性请求、缓存、去重或自动重试，保留原行序和重复记录。

只有原始query而没有准入依据时不自动补造；缺类别的参考数据仍可用准入工具观察文本判断，但不能直接送入质量复测。类别不匹配或未准入的成功判断可以作为独立质评边界样例，不假装成生产最终成员。

产物为 run.json、results.jsonl、report.md、results.xlsx、quality_annotation_prompt.md。保存实际完整输入、Prompt及模型来源哈希、原始响应、四字段结果、错误和耗时。Excel分列展示四字段和准入路线/结论，低分标黄、工程错误标红；公式样式query始终按文本保存，JSONL为完整内容。

低分是合法复核结果，返回0；输入错误、非法响应或请求失败按行记录且结果为空，有错误返回2；启动/文件错误1，中断130。run.json区分低分数和工程失败数，不计算准确率。

## 产物与状态

run.json 保存模式、输入与策略哈希、版本、计数和非敏感模型元数据；results.jsonl 保存来源、独立输入、原始响应、特征、类别、stage_status、observation_provenance、聚合结果和错误。report.md 为可读明细，results.xlsx 为可筛选表格；另有两份 Prompt 快照。重筛模式的 Prompt 元数据是工具当前模板，不表示再次执行；原模型来源以每条 observation_provenance 为准。

route 是文本类型，admission_pass 含完整策略要求；工程失败时二者为 null。类别阶段失败仍保留成功特征。Excel 的1/0/空白分别表示准入/未准入/工程未判定；query及响应按文本保存，不执行公式。XLSX长单元格截短，完整内容以JSONL为准。

features 严格包含 intent_label、intent_reason、open_points、dependencies。类型为 task/dialogue/undetermined；最终交付由 准入评估 映射 task/qa，Stage2 不再判断类型。dialogue 即使有 open_points 仍走 direct；task 的必要澄清才走 clarification。dependencies 每项只含 source、description，用户待定事项不重复写作依赖。

JSONL 保存 admission_group，Excel 分列展示类型、原因、澄清点、依赖和五种分组。统计包括五组准入结果、目标类型、文本路线和全部阻断原因；只有成功完成的判断参与五组统计，工程失败单列。无类别参考集默认未准入时，仍可从文本类型和路线观察识别效果。重筛校验当前四字段特征及策略版本2；旧七字段观察需重新识别，不能自动补造新字段。

退出码：0全部完成（可含正常排除）；2有单条工程失败；1配置、目录或文件错误；130用户中断。无人工真值，不报告准确率。这里的文本筛选不认证正式用例已就绪，正式测试仍需构建环境、判据和证据。

生产 Stage2 默认必跑下游质量标注；原可选难度/语义障碍模块已[归档](../artifacts/archive/v4_pre_downstream/ARCHIVE.md)。准入独立复测不调用该标注。正式交付是 downstream_labeled_v1 的29字段；本工具的审阅 JSONL/Excel 保留自身明细，不等同于正式交付文件。新增下游契约、评分与冻结续跑验收见 tests/test_downstream.py。
