# Stage1 / Stage2 评判标准

核对日期：2026-10-28；质量契约 11。Stage1 只确认文本筛选条件，不证明正式用例已经具备环境实例、评分依据或执行证据。

## Stage1 的三个职责

1. 特征模型只接收 `sample_type`、完整有序的 `conversation_turns`，识别文本、锚点、目标、对象、约束、待确定事项和必要依赖。不得输出准入结论，也不看类别或历史标签。
2. 类别模型独立接收相同会话与 `candidate_category={L1,L2}`，只输出 `category_fit=exact/partial/mismatch`。缺参数或附件不自动使类别错误。无类别参考记录标 `not_provided`，跳过模型请求，不伪造类别。
3. 程序按冻结策略生成 route、admitted 和原因。生产仍要求类别 exact。

## 识别字段

`text_state` 为 recoverable/broken；`anchor` 为 task/topic/dialogue/none。只有主题时不补造介绍、查询、设置或购买目标。`goal` 为恢复出的任务/交流诉求或 null；`objects`、`constraints` 为文本中仍有效的内容。

`open_points` 记录用户尚需确定的 intent/action/parameter/object/conflict。非必要偏好不自动成为缺槽。每项包含 kind 和 description。

`dependencies` 的 source 为 provided、user_to_specify、public_information、system_app_state、current_screen_media、identified_device_resource、missing_material、missing_context、unknown。每项包含 source、description、locator；运行时 locator 必须是输入中已有的非空连续原文。

`objects` 和 `constraints` 保留给人工检查对象与约束的抽取效果，不直接参与准入计算。识别结果与原始 query 一起查看，不额外输出原文摘录字段。

## 策略顺序

| 条件 | route |
| --- | --- |
| 文本不可恢复、无锚点、缺必要材料或背景、依赖来源未知、所需运行时能力未启用 | excluded |
| 无上述阻断，存在明确主题但无操作目标，或有必要澄清事项/用户待定信息 | clarification |
| 无上述阻断，目标成立且必要信息已提供或可在约定环境获取 | direct |

route 独立于类别。route 被策略禁用、类别缺失/partial/mismatch 时仍不准入，但不能把其文本类型改写为语义无效。原因按固定顺序全部保留。

当前策略同时允许 direct 和 clarification。明确商品、人名、地点和功能主题都可用于意图澄清；“闹钟”不等于“设置闹钟”。“嗯”“这样”“他说也不错”和无上下文导航播报不因可接话就获得需求锚点。

缺失的图片、正文、列表、原稿和前序结果不能包装成普通澄清。当前页面、播放内容、公开信息、系统状态、具有明确选择条件的设备资源可作为运行时依赖；“那个文件”不能任意解释为可读取的当前资源。不得断言缺失是采集造成。

确认可继承已明确的前序任务；纠正替代旧条件、取消不再累计，不补造助手回复、历史执行状态或未来用户回复。无关前序不阻断完整新目标。

## Stage2 与 Challenge

Stage2 输入和判断规则保持不变，只输出 task_difficulty、intent_label、execution_issues。task/qa 仍依据原始完整有效任务，不能把所有澄清样本强制改为 qa。它不再重复否决 Stage1 或 executable。双方差异是复核信号，不是人工真值误放行率。

Challenge 接收必要的 stage1_features，不接收策略结论；歧义可以需要实质澄清，但普通补槽、单独主题、低频和澄清本身不等于挑战。八种类型、配额与优先级不变。

## 下游与正式测试

`intent_label` 直接来自原 Stage2。`executable=1` 要求策略准入、direct、单轮、目标轮超过两个字符、风险词未命中；否则为 0，但样本仍交付。系统就绪是假设，不是执行成功结论。

后续用例构建负责具体环境实例、交互终止规则、最低通过标准、不可接受行为、实时答案来源和设备状态证据。本阶段不生成已验证的正式判据。
