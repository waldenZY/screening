# 多数据批跑脚本

入口为 screening/run_screening_batch.sh，脚本放在screening目录内。需要Bash 4+（Linux或Git Bash），运行前激活moto；也可用SCREENING_PYTHON指定moto环境中的Python可执行文件。脚本从screening的父目录调用python -m screening.run_pipeline，不依赖启动时的工作目录。

## 配置块

仅修改脚本顶部用户配置：

~~~bash
DATASET_DIRS=(
  "/data/source/261001-1007-7days"
  "/data/source/261008-1014-7days"
)
SOURCE_FILENAME="cluster.jsonl"
TAXONOMY_FILENAME="taxonomy.json"
RISK_KEYWORDS="/data/common/risk_keywords.xlsx"
OUTPUT_BASE="batch_outputs"
PUBLISH_BASE="/opt/huawei/data3/l00770403/data"
PUBLISH_SUBDIR="evaluation-set-large"
CONFIG_FILE="artifacts/config/pipeline.default.json"
~~~

所有相对路径按脚本所在screening目录解析。每份输入目录须有相同的source/taxonomy文件名，目录最后一段作为数据集名；同名目录会因输出冲突被拒绝。接入下游周目录时，输入目录名应符合YYMMDD-MMDD-Ndays，脚本不自动生成或转换日期标签。

默认输出为screening/batch_outputs/{数据集名}/；默认发布为screening/batch_publications/{数据集名}/evaluation-set-large/。按上例修改PUBLISH_BASE后，发布到下游data/{周目录}/evaluation-set-large/。若已有自定义消费路径，可将PUBLISH_SUBDIR设为空以保留旧的base/{数据集名}/布局。

CONFIG_FILE默认适配v11配置，强制包含admission、category、challenge、quality_annotation四份Prompt。默认本地模型配置为screening/artifacts/config/llm_config.local.json，脚本不保存或打印密钥。

## 检查和运行

从项目根目录执行：

~~~bash
conda activate moto
bash screening/run_screening_batch.sh check
bash screening/run_screening_batch.sh run
~~~

check只做本地只读检查：Bash/Python、输入存在性、名称冲突、新版配置和策略、四份Prompt占位符、风险词表、依赖及模型配置格式。不会调用模型、创建run或发布。正式运行时，pipeline仍会做目录可写检查与一次模型连通性请求。

run顺序处理数据集；每个数据集内部照常并发构建P0/P1/P2，执行必跑质量补标并发布29字段v4文件。一份数据失败继续下一份，最终统一汇总。run/start会发布，不是dry-run。

~~~bash
bash screening/run_screening_batch.sh start
bash screening/run_screening_batch.sh status
~~~

start通过nohup启动Bash worker，不要求脚本具有可执行位；status显示进程状态、成功/失败数量及日志路径。不支持并行启动两个批次。状态与日志保存在screening/.screening_batch/runs/{批次号}/，summary.tsv逐数据记录退出码、源文件、输出/发布目录和日志。

批次全部成功返回0；配置错误或任一数据失败返回1；具体pipeline退出码记录在summary.tsv。FAILED_INPUT为输入文件检查失败；FAILED_PIPELINE为pipeline失败。pipeline分支部分失败可能已发布成功分支，沿用主链路规则。

再次运行按pipeline契约自动恢复或复用兼容run；整体failed的run仍需显式--run-id，可从对应数据的output/runs定位后用主入口恢复，脚本不绕过这条规则。改Prompt或规则需新run。

## 已执行的离线验证

在moto环境，用实际Bash执行临时复制脚本及真实pipeline，注入模拟模型传输：验证只读check、拒绝旧配置、两份数据各发布三套29字段文件、前一份失败后继续下一份，以及后台start/status。没有使用真实业务数据或模型，也没有发布到真实下游目录。
