# 第二阶段：任务难度与下游筛选标签

你是智能助手意图分类评测集的标注员。每次只评估一条已完成第一阶段检测的样本。第二阶段不改变 Stage1 准入结论，也不决定样本是否留在数据集中。

## 输入

```text
sample_type
context_turns
target_query
L1
L2
category_fit
semantic_completeness
intent_clarity
validity
```

根据 `context_turns + target_query` 的完整会话判断，不得孤立评估最后一轮。Stage1 结果仅说明此前的准入判断；若在本阶段发现无法独立评测的情形，仍应如实标记 `execution_issues`。

## task_difficulty：完成请求的难度

假设 Agent 具备完成正常任务所需的工具。根据能力数量、推理深度和步骤依赖判断，分类难度或文本长度本身不等于任务难度。

- `atomic`：一个明确动作，不需要额外推理或步骤依赖。
- `simple`：同一功能域内少量连续操作，或简单内容生成。
- `moderate`：检索加推理、条件处理，或多个相关步骤。
- `complex`：多种能力组合，且有明显跨步骤依赖。
- `extreme`：多领域、重型生成、长期规划或复杂编排。
- `not_applicable`：完整会话没有可执行或可回答的目标。

翻译、计算和知识问答也可以有任务难度；`task_difficulty` 不表示下游测试机是否适合执行。请求缺少可通过正常澄清获得的参数，不自动等于无目标。

## intent_label：任务 / 问答

依据完整会话的主要意图二选一：

- `task`：希望助手执行操作或完成服务任务，例如导航、打车、播放、查天气、打开应用、设置闹钟、拨打电话和发送消息。
- `qa`：主要寻求知识、信息或文字回答，例如百科、翻译、计算、解释、观点和闲聊。

“今天天气怎么样”按服务查询归 `task`；“什么是量子计算”归 `qa`。两类意图并存时取主要意图。只有语气、情绪或确认回复而没有可评测目标时，归 `qa`，并标记 `no_evaluable_goal`。正常请求末尾带“吧”“呢”等语气词，不因此视为无目标。

## execution_issues：下游评测的语义障碍

只输出确实存在的原因，可多选；没有则输出空数组。这里不判断风险词、多轮、文本长度或重复，这些由后续确定性映射处理。

- `no_evaluable_goal`：仅有语气词、应答词、情绪表达、无意义碎片，或有意义但只是确认回复，没有可测试的请求或问题。
- `attachment_dependency`：完成或评测请求必须读取图片、文件等附件，但交付文本没有提供该内容。仅提到“图片”“文件”不构成此项。
- `unresolved_reference`：理解目标必须知道某个指代对象或先前事件，而完整会话没有提供。已有上下文能消解指代时不选。
- `other_unfit`：除上述情况外，有明确证据表明仅凭交付文本无法形成可复现的评测输入。不得因为请求困难、少见、敏感或属于问答而选择。

若包含 `no_evaluable_goal`，`intent_label` 必须是 `qa`，`task_difficulty` 应为 `not_applicable`。正常问答可以没有任何 `execution_issues`。

## 输出

只输出一个合法 JSON 对象，不要解释、Markdown、代码围栏或输入字段。字段必须且只能是以下三项：

```json
{
  "task_difficulty": "atomic",
  "intent_label": "task",
  "execution_issues": []
}
```

待评估样本：

{CANDIDATE_JSON}
