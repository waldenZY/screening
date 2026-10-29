# Stage1 与可选标注评判标准

核对日期：2026-10-29；质量契约 13，筛选策略版本 2。Stage1 确认文本筛选条件，不证明正式用例已具备环境实例、评分判据或执行证据。

## Stage1 分工与四字段

文本模型只接收 sample_type 和完整有序 conversation_turns，输出 intent_label、intent_reason、open_points、dependencies；不接收类别、历史标签或策略，也不输出准入结论。类别模型独立接收会话和候选 L1/L2，只输出 exact/partial/mismatch；无类别参考记录跳过该请求并记 not_provided，不能伪造类别或因此认定语义无效。

| 字段 | 规则 |
| --- | --- |
| intent_label | task / dialogue / undetermined，按当前仍有效的预期结果判断 |
| intent_reason | 非空，一句话解释类型依据，不输出长篇推理 |
| open_points | kind、description 数组，kind 为 intent/action/parameter/object/conflict；只记具体待确定事项 |
| dependencies | source、description 数组，每项来源单选；描述必要信息和来源依据，可有多项 |

dialogue 包括信息查询、知识解释、交流、推荐，以及只在回复中完成的文字生成、总结、规划、翻译、计算。需要检索或读取设备信息并不使它成为 task。task 是实际操作、服务办理、应用资源修改、文件或媒体的处理和生成。混合请求只要有明确且仍有效的操作，整条归 task。

“写请假邮件”“查北京天气”“总结已提供文字”是 dialogue；“写邮件并发送”“打开天气应用”“压缩图片”“生成图片”是 task。“闹钟”“视频剪辑”“临沂”等纯主题、无上文“嗯”“这样”“他说也不错”及导航播报片段为 undetermined，不能靠泛泛追问放行。实质情绪交流可以是 dialogue。“帮我设闹钟”已有操作需求，可澄清时间。

上下文中的确认可继承未结束需求，纠正替换旧条件，取消不再累计；无关旧话题不阻断新需求。不补造助手回复、历史执行状态或未来用户回复。

## 依赖与澄清边界

来源为 provided、public_information、system_app_state、current_screen_media、identified_device_resource、missing_material、missing_context、unknown。每项只输出 source 和 description；用户待确定事项只进入 open_points。

运行时来源依次表示公开信息、系统应用信息、明确当前页面/播放内容、有选择条件的设备资源。默认工具、账号和正常权限就绪，不代表已读取或执行成功。运行时资源必须有文本指向，依据写在 description 中，由模型判断并供人工复核；程序不认证资源真实存在。

缺失特定材料、原列表、前序结果和未知“那个”不能包装成普通澄清，也不能用新检索替代原对象。“总结当前视频”可依赖运行时读取；“总结这份文件”却没有正文或可定位资源时，应记录缺失材料。不得断言缺失由采集造成。必要材料缺失与可澄清参数可以并存，排除优先。

open_points 不无限罗列可选偏好。dialogue 即使有待确定事项也可给通用、条件性回复或合理追问，不强制先澄清；task 的必要参数不齐时进入澄清测试，不能声称最终操作条件已齐备。

## 程序筛选与五组结果

1. undetermined、missing_material、missing_context、unknown 或所需运行时能力未配置：route=excluded。
2. 无上述阻断，dialogue 一律 route=direct；task 有 open_points 为 clarification，否则 direct。
3. 再执行 allowed_routes 和类别要求；生产必须 exact。类别或策略排除不改写文本路线，全部原因按固定顺序保留。

五组互斥统计为 dialogue_without_open_points、dialogue_with_open_points、task_direct、task_clarification、excluded。前四组必须准入，第五组汇总全部策略排除且保留原类型、澄清点和依赖。工程失败单列，不计为排除。报告同时保留目标类型及路线统计，缺类别参考集不能仅看第五组就认为全部文本无效。

## 可选附加标注、Challenge 和交付

Stage2 默认关闭，代码、Prompt、示例和测试集中在 quality/optional_stage2。显式启用时只接收原始会话，输出难度和语义障碍到独立附加文件；不进入正式交付、主报告或成员判断。具体规则见隔离目录的 Prompt，默认流程无需该模块存在。

最终标签只有统一程序映射：dialogue→qa，task→task；undetermined 不交付。Challenge 参考新四字段和原文，不接收策略；澄清、实体或低频本身不构成挑战，既有八种类型及配额不变。

executable=1 要求已准入、direct、单轮、目标轮超过两个字符、冻结风险词未命中。dialogue 有澄清点仍可为1；task 澄清、多轮、短文本或风险命中为0，样本仍可交付。该标记不证明产品操作成功。

后续用例构建负责具体环境、交互终止规则、最低通过标准、实时答案来源和设备状态证据。本阶段不生成并认证正式判据。

三个数据集正式交付均为 labeled_v8 的相同14字段，删除 quality_task_difficulty，不用 null 或 not_applicable 代替未执行。可选 Stage2 不影响最终标签、准入、成员与 executable。
