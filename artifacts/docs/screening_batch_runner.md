# 多数据批跑

入口 screening/run_screening_batch.sh，需要Bash4+和moto环境。修改脚本配置区的DATASET_DIRS、SOURCE_FILENAME、TAXONOMY_FILENAME、RISK_KEYWORDS、OUTPUT_BASE、CONFIG_FILE及PYTHON_BIN。相对路径按脚本目录解析，数据目录末级名作为dataset-id，同批不允许重名。

取消PUBLISH_BASE/PUBLISH_SUBDIR。每份数据的结果为<OUTPUT_BASE>/<dataset-id>/<run-id>/internal与external/evaluation-set-large，不另复制交付。

```bash
conda activate moto
bash screening/run_screening_batch.sh check
bash screening/run_screening_batch.sh run
bash screening/run_screening_batch.sh start
bash screening/run_screening_batch.sh status
```

check只读检查配置v12、四份Prompt、依赖和输入，不调用模型、不创建run。run顺序执行数据集，每份内部P0/P1/P2并发；一份失败继续下一份。start使用nohup，status展示进程与批次状态。

日志位于screening/.screening_batch/runs/<batch-id>。summary.tsv记录dataset、退出码、source/taxonomy、output_root、实际run_path、external_path、completion_status及日志路径。启动前失败的run路径可为空；有external路径不表示整包完成。

批次全部成功返回0，任一失败或配置错误返回1，具体pipeline退出码见summary.tsv。正常pipeline0、分支失败2、启动或整包复验失败1。再次批跑恢复或复用兼容运行；failed运行需使用主入口指定--run-id。Stage1独立复用通过主入口--stage1-from完成。
