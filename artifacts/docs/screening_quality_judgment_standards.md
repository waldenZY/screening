# 准入评估与下游质量标注标准

核对日期：2026-10-30。quality_contract_version=14，quality_rubric_version=1。Stage1表示整个采样集构建，Stage2表示整个下游交付适配；模型功能分别称准入评估和下游质量标注。

## Stage1中的准入评估

文本特征模型只接收sample_type与完整有序conversation_turns，输出intent_label、intent_reason、open_points、dependencies。类别模型独立接收会话和候选L1/L2，输出exact/partial/mismatch；不互相传入判断。程序再按冻结policy生成路线和准入。

需求类型：dialogue包括信息、解释、推荐、交流及只在回复中完成的写作、总结、规划、翻译和计算；task包括实际操作、服务办理、应用资源修改、文件或媒体处理生成；undetermined表示无法确定有效需求。混合请求的有效操作优先。

必要依赖来源为provided、public_information、system_app_state、current_screen_media、identified_device_resource、missing_material、missing_context、unknown。可获取的运行时来源必须有文本依据和冻结能力约定；不认证资源真实存在。不把缺失原材料当成普通补槽。

无硬阻断时dialogue走direct，task有具体待确定事项走clarification，否则direct。undetermined、必要材料/背景缺失、未知依赖、未配置运行时能力优先excluded。类别或策略不满足仍不准入，但不改写原文本路线。生产必须exact，direct和clarification均可准入。

完整会话用于恢复当前有效需求，纠正替换旧条件、取消不累计，不假定旧请求已执行。纯主题、无背景确认与导航播报不补造成需求；有实质交流诉求的情绪表达可以是dialogue。详细模型边界见当前quality_admission.md及quality_admission_category.md。

## Stage2中的下游质量标注

只对最终冻结成员标注；输入完整会话、L1/L2、准入特征、类别、策略和结论。先前准入供核查，不是必须沿用的满分答案。Prompt是[quality_annotation.md](../prompts/quality_annotation.md)，包含各枚举正反例和严格一致性协议。

| 字段 | 保持的取值与含义 |
|---|---|
| quality_validity | valid：直接响应或澄清场景可稳定评测；borderline：目标存在但边界有实质不确定性；invalid：无目标或严重缺失等导致无法稳定评测 |
| quality_intent_clarity | clear：有效需求及应响应/应澄清事项足够确定；vague：影响评测的多义目标或边界；no_intent：无有效需求 |
| quality_overall_score | 整数1～5，不按任务难度、是否多轮、风险词或澄清路线机械降分 |
| quality_issue_types | 原枚举非空无重复数组，无问题为["none"] |

评分：5=valid+clear+none且类别无复核异议；4=valid且有轻微具体问题；3=borderline、目标仍可识别且需复核；2=invalid但目标仍可识别，存在严重缺陷；1=invalid+no_intent，无法识别可靠有效目标。低于5分必须有具体问题；no_intent当且仅当1分。1分必须有nonsense/emotional_only/too_brief/no_evaluable_goal之一的文本证据。

## 问题枚举的新定义

| 取值 | 判据 |
|---|---|
| none | 无影响该场景稳定评测的问题，只能单独出现 |
| category_mismatch | 候选类别与有效目标不符或只覆盖部分，非分类困难 |
| ambiguous_intent | 目标或澄清边界存在影响评测的实质歧义，非普通缺参数 |
| context_dependent | 必需材料/背景缺失且不属于明确约定可获取资源，包括无法消解指代；非正常补槽 |
| truncated | 影响当前需求恢复的截断残缺，非可恢复口语 |
| nonsense | 完整会话仍无可恢复有效语义 |
| emotional_only | 只有情绪碎片且无法识别交流或其他目标，非实质情绪交流 |
| too_brief | 过短且无有效语义，非短而完整指令 |
| no_evaluable_goal | 文本有意义但没有仍有效目标，只用于1分 |
| multi_intent | 多个独立目标造成稳定评测或类别边界问题，非连贯清晰的多步骤组合 |

## 必须覆盖的边界

“帮我设闹钟”目标和补时间的事项明确，可5分；不因clarification扣分。“查北京明天天气”可依赖公开信息；“总结当前视频”可按明确当前资源和能力约定评估，不臆造已读取结果。“我很难过，陪我聊聊”可以是有效交流；“暂停”短而完整仍可高质量。

“明早七点设闹钟|改成八点”按纠正后目标；问题旧目标被明确取消，不再影响完整新需求。多轮、低频、复杂步骤或风险动作本身不降分。清晰连贯多步骤不自动multi_intent。

“总结这份文件”缺正文且无可定位资源，复核可给invalid/clear/2/context_dependent；不能因已经准入掩盖问题。无前序“好的谢谢”为1分/no_evaluable_goal；继承未结束目标的确认不因此无效。

## 标注结果与交付

quality_category_fit直接来自准入类别核验，不由补标覆盖。dialogue→qa、task→task；executable按现有单轮direct等规则计算。评分低及跨阶段分歧照常交付，不修改成员、标签或executable。报告记录评分、问题分布和复核信号，不声称人工真值准确率。

缺字段、非法枚举、字段矛盾或请求失败按工程失败重试；不以null、默认5分或none掩盖。失败分支不发布，同run可恢复冻结成员与成功缓存。评分标准升级时冻结新版本及Prompt，旧分数不解释为同一口径。
