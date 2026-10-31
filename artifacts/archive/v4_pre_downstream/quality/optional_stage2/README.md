# 可选 Stage2 隔离模块

本目录集中保存难度与语义障碍的标注逻辑、Prompt、示例及专用测试。默认不导入、不读取 Prompt、不请求模型、不创建 Stage2 账本或检查点。删除本目录不影响默认链路和 Stage1 独立复测。

生产配置使用 `optional_stage2={"enabled":false,"prompt":null}`。如需显式启用，设置 enabled=true，或使用 `--enable-stage2`；prompt=null 时使用本目录 prompt.md，也可用 `--stage2-prompt` 指定文件。只指定 Prompt 不隐式启用；`--disable-stage2` 可覆盖配置。启用却缺模块或 Prompt 时明确报错。切换开关或已启用的 Prompt 会创建不兼容的新 run，不允许套用旧检查点。

本模块通过通用附加标注接口复用并发门、缓存、重试和请求账本。最终成员确定后标注，只接收原始会话，输出 task_difficulty、execution_issues。结果位于 `<run>/P0|P1|P2/optional_annotations/stage2.jsonl`，汇总位于 stage2_report.json；请求账本位于 `<run>/quality/stage2_*.jsonl`。发布复验核对独立任务、输入和结果，附加结果不写入正式交付或主质量统计。

P0/P1/P2 始终使用 labeled_v8 的14字段，开关不改变类型、准入、成员和 executable。显式要求的附加标注若工程失败，该分支标为未完成，可沿用缓存重试；不会把失败伪装成“无语义障碍”。

本目录专用测试从项目根目录在 moto 环境执行：

```powershell
python -B -m unittest discover -s screening/quality/optional_stage2 -p "test_*.py" -v
```

若未来彻底删除，保留默认关闭即可；配置开关和三处惰性接入点可一并移除。主链路的交付、筛选和报告逻辑无需恢复任何旧字段。示例用于规则复核，不表示真实模型准确率。
