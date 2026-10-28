# Screening v4 当前技术设计

核对日期：2026-10-28。产品名为 Screening v4，交付格式升级为 labeled_v6；版本表见 [文档索引](README.md)。旧 run 不按新标准续跑，回退点为 `baseline/pre-stage1-redesign-2026-10-26`。

## 数据流

cluster JSONL + taxonomy → 原有 canonical 索引与采样特征 → P0/P1/P2 独立并发分支 → 集合内去重 → Stage1 特征识别 + 独立类别核验 → 冻结策略筛选 → P1 配额补采 / P2 Challenge 与预算内自适应补采 → 最终成员 → 原 Stage2 → executable → v6 导出与复验。

原始会话解析、索引、概率/频次/长度/复杂度特征、P0/P1/P2 配额算法不变。去重仍在 Stage1 前：规范化精确去重，以及同 L2 下字符双字召回、冻结 IDF 加权 2/3-gram Jaccard 和编辑相似度确认。阈值仍为 0.45/0.80/0.90，浮点边界容差 1e-12。跨集合重叠允许并报告。

## Stage1 契约

- `stage1_features` 请求仅使用 sample_type 和完整 conversation_turns；特征不受类别或策略影响。
- `stage1_category` 请求增加候选 L1/L2，不接收特征、筛选结果或旧标签。
- 程序聚合为 `{"stage1":{"features":...,"category":...,"policy":...,"screening":...}}`。screening 包含 route、admitted、reasons、policy_sha256，读入时按保存的 policy 重新计算并核对。
- 校验模型输出结构与运行时资源选择条件。特征、类别与策略都有独立校验器，保留 objects、constraints 供人工分析抽取效果。详见 [判定标准](screening_quality_judgment_standards.md)。

生产筛选仍要求类别 exact。direct 和 clarification 都进入成员选择和补采；excluded 保留在质量账本中。缺类别的独立参考样本继续识别文本类型，但生产策略不会把其当成类别已核验的成员。

## 请求、缓存和恢复

两个 Stage1 子阶段共享现有全局公平并发门、独立重试和 SQLite 单次在途去重。特征缓存键为阶段、特征 Prompt、模型选项、纯会话输入哈希；类别缓存另绑定候选类别。任一子阶段失败不丢失另一阶段已缓存的成功结果。

原 stage1 账本改为程序聚合账本，其任务连接 feature_task_id、category_task_id 和策略哈希。质量目录另保存 stage1_features / stage1_category 的 tasks、results、attempts、cache_hits 和 Prompt 快照。策略变化不能当作同 run 续跑；独立模块可读取已保存结果无模型重筛。

Stage2 仍只接收原会话，不接收类别或 Stage1。Challenge 的 payload 和缓存键增加相应 Stage1 特征。Stage2 的 execution_issues 仅作差异复核，不改成员或重复否决 executable。

配置冻结四份 Prompt、策略及运行时能力约定。manifest、质量、内部记录、报告、映射契约均已升级；不兼容旧 run 显式拒绝。原索引仍可按自身契约复用，新 run 不自动共享旧质量缓存。

## 交付与发布

内部 screened_schema_version=9，quality 中包含 stage1 对象和原 Stage2 三项；delivery_labels 包含 intent_label、executable、executable_reasons。executable 只由已准入、direct、单轮、目标长度大于2和冻结风险词未命中决定。

labeled_v6 每条 15 字段，保留原字段并追加 stage1 对象，文件名为 P0_smoke_set_v6_labeled.jsonl、P1_benchmark_set_v6_labeled.jsonl、P2_adversarial_set_v6_labeled.jsonl。`executable=0` 不删除样本。字段见 [v6 字典](screening_labeled_v6_field_dictionary.md)。

发布复验重建 Stage1 聚合结果与策略结论，校验独立子任务、Prompt、输入和哈希关联，再重算 executable。导出与 canonical/provenance 逐条关联。分支发布只要求本分支相关子任务进入终态，不等待其他分支。报告统计 direct/clarification/excluded、各类准入率、类别排除、依赖原因和 executable 置零原因。

保留现有分支检查点、事务发布和冲突拒绝机制：成功分支不因其他失败回滚；全量复验完成后生成整组产物快照。正常退出0、分支失败2、启动或全量复验失败1。`--replace` 才允许替换不同内容。

## 验收

`moto` 环境运行 module_tests/tests。模拟 LLM 验证规则边界、请求隔离、缓存恢复、策略重筛、P0/P1/P2 补采与构建、并发分支发布、v6 导出、复验和旧契约拒绝。真实模型参考复测单独执行，不据无人工真值的记录计算准确率，不自动运行全量或正式发布。

## 当前模块组织

`quality/runner.py` 负责四类模型请求、缓存和重试；`quality/stage1.py` 负责观察契约和策略；`delivery/labeled.py` 负责当前15字段导出与校验。旧 two_stage/legacy 模块和单样本兼容包装已移除。运行使用 labeled_export_report.json、labeled_mapping_version 和 labeled_* 来源哈希；内部 pipeline/manifest版本为6。
