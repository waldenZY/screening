# Screening 三阶段技术设计

## 数据与阶段边界

预处理建立共享SQLite候选索引；Stage1保持原采样、集合内去重、独立特征/类别判断、策略准入、P1补采与P2自适应Challenge选择。最终成员写stage1.frozen.jsonl，数量和哈希进入manifest.frozen。该冻结文件不包含下游补标。

Stage2保持quality_annotation四字段模型任务及评分契约，再由冻结的路线、概率档、规则复杂度计算三个交付维度。类别沿用Stage1。低分及差异不影响成员、intent_label或executable。所有映射输入必须合法，不填默认值。

## 内部事实与外部投影

每次运行是<output-root>/<run-id>。internal保存不可变冻结结果、screened.canonical成品、模型账本、规则与Prompt、补采及审计。完整报告包含build_summary；export_validation集中三个集合的导出检查；manifest包含冻结摘要与交付索引。合并重复报告，Prompt只保存一份，外部文件不在内部重复复制。

external/evaluation-set-large由冻结成员稳定顺序投影基础版，再追加八个质量字段。外部保留intent_label/executable，不携带stage1/provenance对象；export_provenance记录基础/标注/内部记录哈希、行号和任务身份。

## 恢复与独立Stage2

同run恢复由契约绑定的检查点与缓存驱动。完成分支校验文件哈希后复用；冻结分支仅恢复Stage2。删除发布操作，保留来源、输入、模型子任务、Challenge、去重审计、字段映射、报告守恒及哈希复验。

--stage1-from检查新/旧v4来源的索引、输入、Prompt、独立准入任务、Challenge、冻结哈希与恢复状态，复制只读Stage1依据到新run。源冻结原文及来源run-id不改写，本次run与来源契约关系由新manifest记录。当前Stage2契约与Stage1来源契约分开验证。仅兼容输入、Prompt、模型选项和质量契约的四字段结果进入新缓存；不得将来源branch_complete作为新交付已完成。

## 报告与完成

采样报告按最终基础文件计数，all_tags为三个文件合计，跨集合重复分别计数。L2按(L1,L2)组合。P2输出真实自适应过程，不编造旧双轨计划。采样耗时定义为Stage1分支墙钟耗时最大值，包含准入/Challenge；旧运行无计时依据时为null，另保留当前构建及补标耗时。

质量报告覆盖八字段，切片保留labeled原行，允许重叠，零条写空文件。每分支可独立完成但整包必须全部复验通过才completed；失败保留已完成分支，续跑不撤销。该完成状态同时记录于manifest.status和delivery.status。
