# 配置、输出与恢复

配置版本12。使用 artifacts/config/pipeline.default.json；移除了 publication。旧版本配置明确拒绝，请更新版本并删除 publication。旧发布参数 --publish-root/--no-publish/--replace 不再接受。

## 配置与参数

inputs 为 source、taxonomy、risk_keywords；outputs 只有 output_root。prompts 为 admission、category、challenge、quality_annotation。保留 index、logging、sampling、p1_supplement、p2_supplement、screening_policy。

默认P1目标6000、P2目标1000，LLM并发10、最大尝试轮数20、请求前等待1秒。风险词、采样、准入、Challenge、评分判据不因交付改造变化。生产要求类别exact。

保留 --config、--source、--taxonomy、--risk-keywords、--output-root、--cache-root、--run-id、--new-run、四份Prompt覆盖、--workers、--max-retries、--sleep、索引参数与 --log-level。相对CLI路径仍按配置目录解析；Stage1来源目录按调用工作目录解析。

新增 --stage1-from <run目录>，与 --run-id/--new-run 互斥。来源可为新版run目录或其internal，也可为改造前v4 run目录。只复用通过校验的完整三集合冻结结果及任务依据，不支持v3；不回退采样。来源Stage1配置、Prompt、策略从原运行加载，禁止通过准入或Challenge Prompt参数覆盖。Stage2配置可变化。冻结风险词足以计算标签，原Excel可离线；源JSONL、taxonomy、索引仍需可校验。

## 运行目录

output_root直接包含run-id子目录及共享_canonical_cache，不再加runs层。每个run包含internal与external。internal/snapshots/config.json保存初始有效配置和分阶段契约；后续调用参数记入manifest.last_invocation，不修改已冻结快照。Prompt只在snapshots/prompts保存一份。

external/evaluation-set-large总是生成，按固定v4文件名导出六个集合文件、采样报告、质量报告、30个切片，创建空可视化目录。报告时间戳来自run创建时间，续跑不新增重复文件。

## 恢复与状态

同契约优先恢复未完成run，再复用完成run；failed需显式--run-id。冻结后失败恢复不重采样、准入或Challenge。Stage1来源运行不被覆盖；新版run可独立使用本地复制的冻结依据。

四字段缓存键保持原规则，绑定完整实际输入、Prompt和模型参数。--stage1-from可导入完全兼容的成功标注结果；变更Prompt或模型只重新调用四字段补标，不改变Stage1。

完成分支可独立保留。仅全部文件、统计与追溯复验成功时status与delivery.status均completed；有目录不表示整包完成。退出0全部完成，2分支失败，1启动或整包校验失败。每次启动仍有固定短文本连通性检查，不提交源Query；日志隐藏密钥和服务地址。
