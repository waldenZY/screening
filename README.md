# Screening v4

从 cluster JSONL、taxonomy 和风险词表构建 P0/P1/P2。链路统一为 **预处理 → Stage1：采样集构建 → Stage2：下游交付适配**。正式交付为 **27 个扁平业务字段 + stage1、provenance 两个对象**，数据契约为 downstream_labeled_v1，文件名按下游要求固定为 v4。详见[字段字典](artifacts/docs/screening_downstream_labeled_v1_field_dictionary.md)。

## 三个阶段

1. **预处理**：输入校验、会话解析、taxonomy 关联、风险词表冻结、候选索引和公共特征计算。公共特征为概率桶、簇内频次桶、归一化频次、长度、规则复杂度和 formality。
2. **Stage1：采样集构建**：计算集合 relevance；P0/P1/P2 独立并发采样、集合内去重、准入评估；P1 按配额补采，P2 做 Challenge 判定及预算内补采。成员和全部判断先冻结，再进入 Stage2。
3. **Stage2：下游交付适配**：对最终成员执行必跑的下游质量标注，计算与映射交付字段，复验并按需发布。低分照常交付，不改变成员、intent_label 或 executable。

原 Stage1 已改称“准入评估”，包括独立文本特征识别、类别核验，以及程序策略准入判断。原可选 Stage2 难度/语义障碍模块已经退出当前链路，见[归档](artifacts/archive/v4_pre_downstream/ARCHIVE.md)。当前不存在可选标注开关。

准入评估只观察 intent_label、intent_reason、open_points、dependencies，并独立核验类别。程序按冻结策略决定 direct/clarification/excluded；生产要求 exact，direct 与 clarification 均可准入。dialogue→qa、task→task。executable=1 要求准入、direct、单轮、目标轮超过2字符且未命中冻结风险词；为0仍交付。

## 配置和启动

Python 操作使用 **moto** 环境，从包含 screening/ 的项目根目录执行：

~~~powershell
conda activate moto
Copy-Item screening/artifacts/config/llm_config.example.json screening/artifacts/config/llm_config.local.json
python -m screening.run_pipeline
~~~

填写[默认配置](artifacts/config/pipeline.default.json)中的 inputs.source、inputs.taxonomy、inputs.risk_keywords。配置版本为11，四份 Prompt 为 admission、category、challenge、quality_annotation；默认 publication.enabled=false。P1 默认目标6000、P2目标1000，LLM全局并发10、最大尝试轮数20，请求前等待1秒。采样规则和预算沿用当前设置。

~~~powershell
python -m screening.run_pipeline --source D:/data/cluster.jsonl --taxonomy D:/data/taxonomy.json --risk-keywords D:/data/risk_keywords.xlsx --output-root D:/data/screening-runs --publish-root D:/data/261020-1026-7days/evaluation-set-large
~~~

可用 --admission-prompt、--category-prompt、--challenge-prompt、--quality-annotation-prompt 覆盖 Prompt。旧 --stage1-prompt、--stage2-prompt、--enable-stage2、--disable-stage2 已移除。只提供完整链路入口，不提供独立阶段运行命令。

风险工作簿要求 Sheet1 第一行依次为“关键词”“风险级别”，后续两列均为非空文本；额外非空列和非法类型会启动失败。对最后一轮匹配核心词与原词，命中任一风险级别即置 executable=0，不剔除成员。风险词表和匹配规则随 run 冻结。

## 产物、续跑和发布

每个分支保存：

- stage1.frozen.jsonl、stage1_freeze_report.json 和 stage1_frozen 检查点：补标前的冻结成员与依据。
- stage2/quality_annotations.jsonl、stage2/quality_report.json：四字段标注、评分分布及复核信号。
- screened.canonical.jsonl：包含冻结 Stage1 与标注结果的内部交付记录。
- *_v4_labeled.jsonl、export_provenance.jsonl、labeled_export_report.json：正式交付与逐条来源复验。

run 根目录保存 manifest、冻结风险词表/去重统计、Prompt 快照、quality/ 请求和缓存账本、汇总报告。公共索引位于输出根目录的 _canonical_cache；不再保存 relevance 列。保留 run 及引用索引以便恢复与复验。

同输入和兼容配置重跑可恢复或复用；--run-id 显式选择原 run，--new-run 新建。已冻结分支补标失败后续跑直接恢复 Stage1，不重新采样、准入或 Challenge；同 run 的成功标注可从缓存复用。Prompt、模型、规则或契约变化需要新 run，不支持跨 run 复用 Stage1。旧配置及旧 run 不迁移。

发布路径：

~~~text
P0-冒烟集/P0_smoke_set_v4_labeled.jsonl
P1-基准集/P1_benchmark_set_v4_labeled.jsonl
P2-对抗集/P2_adversarial_set_v4_labeled.jsonl
~~~

publish_root 应指向下游对应周目录的 evaluation-set-large；不自动推断日期。不再额外导出14字段v8。各分支完成 Stage2 复验后可独立事务发布；其他分支失败不回滚成功发布。同哈希复用，不同内容默认拒绝，--replace 才替换；--no-publish 可临时关闭发布。成功退出0，分支失败2，启动或全量复验失败1。

每次启动先校验四份 Prompt、目录写入及原子操作，并发送一次固定短文本连通性请求；不使用源 Query，失败不重试。这次请求在续跑和完成复用时仍发生，不计入标注账本。日志不打印密钥、服务地址、响应正文或原始 Query。

## 标准与验证

[当前文档](artifacts/docs/README.md) · [质量标准](artifacts/docs/screening_quality_judgment_standards.md) · [配置说明](artifacts/docs/screening_pipeline_configuration_v4.md) · [单模块复测](module_tests/README.md) · [多数据批跑](artifacts/docs/screening_batch_runner.md)

~~~powershell
python -B -m unittest discover -s screening/module_tests/tests -p "test_*.py" -v
~~~

工程测试使用临时数据与模拟 LLM，覆盖阶段冻结、补标失败续跑、字段枚举、缓存隔离、独立发布和篡改检出；不代表真实模型准确率。真实模型边界复测单独执行，不自动运行全量数据或正式发布。下游需求原始记录由业务手动维护，本次未修改。
