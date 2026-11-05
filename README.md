# Screening：采样与下游交付

链路为预处理 → Stage1 采样集构建 → Stage2 下游交付适配。Stage1 的采样、去重、准入、P1 补采和 P2 自适应 Challenge 选择规则保持不变；Stage2 保留已有四字段 LLM 标注，再通过规则补齐三个字段，不增加判断请求。

## 启动

Python 使用 **moto**，从包含 screening/ 的项目根目录执行：

```powershell
conda activate moto
python -m screening.run_pipeline --source D:/data/cluster.jsonl --taxonomy D:/data/taxonomy.json --risk-keywords D:/data/risk.xlsx --output-root D:/data/screening
```

填写 `artifacts/config/llm_config.local.json`（参考 example），或使用项目现有模型配置。配置版本12，不再有 publication；`--publish-root`、`--no-publish`、`--replace` 已删除。交付总是生成在本次运行中，无需发布参数。

## 输出

```text
<output-root>/
├── _canonical_cache/
└── <run-id>/
    ├── internal/
    │   ├── manifest.json
    │   ├── snapshots/           有效配置、契约、风险词、去重依据、唯一 Prompt 快照
    │   ├── quality/             请求账本、结果、尝试和缓存
    │   ├── reports/             完整报告、导出校验、字段使用说明
    │   └── P0/、P1/、P2/        冻结成员、内部成品、审计、追溯和断点
    └── external/evaluation-set-large/
        ├── P0-冒烟集/           基础版与 labeled
        ├── P1-基准集/           基础版与 labeled
        ├── P2-对抗集/           基础版与 labeled
        ├── sampling_report_v4.json
        ├── 质量标注/reports/    八字段分布报告
        ├── 质量标注/subsets/    每集合十个切片，零条仍建空文件
        └── 可视化/             空目录
```

操作者直接取走整个 evaluation-set-large。只有 internal/manifest.json 的 status 和 delivery.status 均为 completed 才表示整包完成；部分分支失败仍保留其他分支和断点。低分与 executable=0 不删行。

基础版 P0/P1 各22字段，P2为23字段；labeled逐行追加八个质量字段，分别30/30/31字段。P2同时提供 relevance_p0/relevance_p2。外部不透传 stage1/provenance 对象，来源留在 internal。

## 续跑与复用

兼容配置重跑自动恢复未完成运行或复用完成运行。`--run-id ID` 显式续跑，`--new-run` 新建。四字段补标失败后直接使用冻结 Stage1，不重复采样或准入。成功0、分支失败2、启动或整包校验失败1。

```powershell
python -m screening.run_pipeline --stage1-from D:/data/old-run --output-root D:/data/screening
```

`--stage1-from` 与 --run-id/--new-run 互斥，新建 run，仅运行 Stage2；支持改造前 v4 和新版完整冻结结果，不支持 v3。来源保持不变。需要源 JSONL、taxonomy、索引及必要依据仍可校验；风险词采用冻结快照，不要求原 Excel 仍存在。来源 Stage1 的配置和 Prompt 保持原值，Stage2 Prompt/模型可变。四字段成功结果仅在实际输入、Prompt、模型参数及质量契约完全一致时复用。

## 文档与验收

[字段字典](artifacts/docs/screening_downstream_labeled_v2_field_dictionary.md) · [配置与恢复](artifacts/docs/screening_pipeline_configuration_v4.md) · [批跑](artifacts/docs/screening_batch_runner.md) · [单模块复测](module_tests/README.md)

```powershell
python -B -m unittest discover -s screening/module_tests/tests -p "test_*.py" -v
```

工程验收使用临时数据和模拟 LLM，不代表真实模型准确率，不自动跑全量业务数据。原始下游需求材料保持原文，实际字段判据以本项目字典为准。
