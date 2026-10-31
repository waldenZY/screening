# Screening v4 配置与运行

核对日期：2026-10-30。Python使用moto，从包含screening/的根目录执行。当前config_version=11，严格校验未知和缺失字段；旧配置不自动迁移。完整默认值见[配置模板](../config/pipeline.default.json)。

## 输入、输出与路径

- inputs.source：cluster JSONL；包含text、session、cluster及可选cluster_prob/frequency等来源字段。
- inputs.taxonomy：当前分类体系文件；必须能映射源cluster。
- inputs.risk_keywords：必填Excel；Sheet1首行为“关键词”“风险级别”，两列非空文本，至少一条有效词条。空行可跳过，额外非空列或非法类型报错。
- outputs.output_root：运行和索引输出根；默认 ../../outputs_v4，相对配置文件目录解析。
- index.cache_root：为空时使用输出根下 _canonical_cache。索引契约已升级，旧索引不按新格式解释。

配置文件内相对路径及CLI覆盖的相对路径均以配置目录为基准。LLM凭证默认来自 artifacts/config/llm_config.local.json（复制example后填写）。

## 三阶段与四份Prompt

prompts必须恰好有admission、category、challenge、quality_annotation：
- admission：Stage1中的准入文本特征识别，默认quality_admission.md。
- category：Stage1中的独立类别核验，默认quality_admission_category.md。
- challenge：Stage1中的P2挑战判定。
- quality_annotation：Stage2中的下游质量补充标注子模块，默认quality_annotation.md，必跑。

原optional_stage2配置及 --enable-stage2、--disable-stage2、--stage2-prompt 已移除。原 --stage1-prompt 改为 --admission-prompt。quality配置仍控制全局并发、重试和请求前等待，所有模型任务共用；默认workers=10、max_retries=20、sleep_seconds=1.0。

screening_policy保持version=2，包含allowed_routes、require_category_exact、runtime_capabilities。默认允许direct和clarification，生产要求require_category_exact=true；默认运行时能力为public_information、system_app_state、current_screen_media、identified_device_resource。改策略需新run。

## 公共特征与集合评分

概率桶high>=0.9、mid>=0.5、否则boundary。frequency桶在完整源cluster内以“严格小于当前频次的记录数/总数”分档：>=0.8为hot、>=0.2为warm、否则cold。norm_freq为簇内log1p频次min-max，全部相同取1。

长度按清洗后完整文本：<10为short，10～30为mid，>30为long。complexity沿用固定启发式，不代表质量或任务难度。formality恢复历史规则，阈值<=0.5 formal、<=1.5 mixed、其余colloquial；保留历史三字符匹配的兼容加分，不在本次修改规则。

Stage1集合评分默认p0=0.6p+0.4f、p2=0.5(1-p)+0.5(1-f)。评分不再存公共索引，成员冻结时保存原精度；norm_freq和对应relevance只在交付时round至4位。

## 采样与补采

sampling包含p0、p1、p2、dedup；p1_supplement、p2_supplement沿用模板全部字段。
- P0：默认大L1阈值3000，大/小L1分别选3/2个L2，每cluster最多5，回退池10，MMR lambda=0.7。
- P1：目标6000；概率权重high/mid/boundary=3/2/1、频次hot/warm/cold=2/2/1、cold_boundary_floor=1、MMR lambda=0.7。补采默认最多3轮。
- P2：目标1000；复杂/多轮/边界/探索来源占比0.3/0.3/0.2/0.2，最低规则复杂度moderate，边界概率cap=0.5，每批1000。类型软下限及max_per_primary=350沿用模板。补采最多3轮，总抽样预算20000、尾部1000、尾部单批250、连续无进展上限2。
- dedup：精确文本规范化、同L2近重复判定；召回Jaccard0.45、加权Jaccard0.8、编辑相似度0.9。规则哈希和全量源IDF被冻结，跨集合重叠允许。

index运行参数包括batch_size、progress_every、sqlite_cache_mb、min_free_space_gb、reserve_free_space_gb、verify_full_source_hash。默认批50000、进度100000、缓存1024MB、最低空间10GB、保留8GB、全量源哈希复验false。

## 启动与续跑

~~~powershell
conda activate moto
python -m screening.run_pipeline --config screening/artifacts/config/pipeline.default.json
~~~

CLI覆盖：--source、--taxonomy、--risk-keywords、--output-root、--publish-root、--no-publish、--replace、--run-id、--new-run；四个Prompt覆盖为 --admission-prompt、--category-prompt、--challenge-prompt、--quality-annotation-prompt。并发和运行参数见 --help。

每次启动先检查四份Prompt占位符、输出/索引及发布目录的写入和原子操作，再发送一次固定短文本连通性请求；失败直接退出1。该请求不使用数据Query，完成复用和续跑仍发生。

同输入和兼容契约重跑先恢复可续跑run，再复用完成run；failed状态需显式 --run-id。Stage1冻结后补标失败，沿原run恢复成员、成功补标缓存和其他完成分支，不重新采样或请求准入/Challenge。改变Prompt、模型、规则或其他冻结参数需要新run；不支持跨run复用Stage1，也不提供独立阶段CLI。

## publication发布参数

publication={enabled:false,publish_root:null}为默认。启用时publish_root必填，应为：

~~~text
$EVAL_DATA_ROOT/data/{YYMMDD-MMDD-Ndays}/evaluation-set-large/
~~~

日期标签由调用者提供，不从数据或当前时间推断。正式文件为P0-冒烟集/P0_smoke_set_v4_labeled.jsonl、P1-基准集/P1_benchmark_set_v4_labeled.jsonl、P2-对抗集/P2_adversarial_set_v4_labeled.jsonl。v4仅为下游固定名称，实际契约为downstream_labeled_v1；不另外输出v8。

分支补标及复验成功才发布。同哈希复用，不同内容默认拒绝，--replace显式事务替换。分支失败返回2且成功分支仍有效；启动/全量复验错误返回1；成功0。事务发布不删除run工作区。保留run和其引用索引以便后续复验。
