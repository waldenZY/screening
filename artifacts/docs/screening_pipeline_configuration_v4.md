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

## 索引性能日志与兼容性

单盘优化保持配置版本12、候选schema3、索引版本4及特征规则4。配置项index.sqlite_cache_mb默认1024，index.batch_size默认50000，synchronous=FULL不变。扫描期始终维护去重约束，新的查询索引延后建立；频次回填改为一次候选表更新，不改统计总体或浮点公式。

throughput为本次扫描平均速度，interval_throughput为上次进度日志到本次的速度；rows_per_second为区间行速率。read/sqlite/checkpoint/other_seconds分别为该区间源读取、数据库操作、保存进度及其他处理的耗时。前缀SHA校验发生在扫描计时之前，有独立日志。IDF的排序准备和文档读取另有日志。

已完成缓存直接复用；同版本中断缓存继续使用原检查点。扫描中已有的旧查询索引不删除，最终特征及集合选择不变。建造索引、回填和最终清理中断后可以恢复。不要为尝试优化而删除旧缓存或改动正在运行的数据库文件。

## 单盘运行的参数选择

下列参数可放在完整配置的index对象中，也可由对应CLI参数覆盖。它们控制建索引的资源与记录频率，不改变采样或模型判断标准。

| index配置项 | CLI参数 | 当前默认值 | 含义与建议 |
| --- | --- | ---: | --- |
| sqlite_cache_mb | --index-cache-mb | 1024 | SQLite缓存额度，单位MiB；常用数据库页可留在内存中。共享服务器可先试4096，默认值未改 |
| batch_size | --index-batch-size | 50000 | 每处理这些源行提交并保存恢复进度，先保持默认 |
| progress_every | --index-progress-every | 100000 | 每处理这些源记录打印扫描进度，先保持默认；不控制保存频率 |
| min_free_space_gb | --index-min-free-space-gb | 10 | 建索引启动时的最低剩余空间，单位GiB |
| reserve_free_space_gb | --index-reserve-free-space-gb | 8 | 扫描批次提交后检查的剩余空间安全线，单位GiB |

例如，在原有完整启动命令末尾追加 `--index-cache-mb 4096`，会在下次启动时尝试约4 GiB的数据库缓存。缓存按需使用，不等于整个Python进程的内存上限；多个并行建索引进程分别占用资源。先确认该任务可使用的内存额度，整机有空闲内存不代表任务没有单独限额。Linux本身也会缓存文件，增加该值不保证等比例提速。

先只调整缓存并比较相同阶段的日志。若checkpoint_seconds确实占很大比例，再单独试batch_size=100000；保存间隔越长，中断后可能需要重做的行数越多。若sqlite_seconds占主要时间，应优先继续减少数据库访问；若read_seconds占主要时间，应检查源文件所在盘的读取竞争。quality.workers只控制模型任务并发，不会并行加速候选索引构建。

批跑脚本没有单独的索引参数区。需要批跑试用时，复制完整默认配置为一份专用配置，修改其中index.sqlite_cache_mb，并将脚本CONFIG_FILE指向该配置。配置内相对路径按配置文件所在目录解析，复制到其他目录时需同步核对Prompt等相对路径。不要在JSON中填写内部函数名cache_mb；有效键名是sqlite_cache_mb。

10/8 GiB是空间检查阈值，不是“200GB输入只需10GB空闲”的容量保证；数据库、写入日志及排序临时文件还会占用空间。当前保留WAL写入方式、synchronous=FULL保存保护及temp_store=FILE；本轮未开放或修改这些设置。已在运行的旧进程不会自动获得新代码或参数，已完成缓存仍优先复用。离线冒烟步骤见[测试说明](../../module_tests/README.md#整体冒烟测试)。
