# 当前文档

契约：config_version=12，pipeline/manifest=10，labeled_mapping_version=11，labeled_schema=downstream_labeled_v2，report_schema=15。Stage1 build配置版本9、候选schema3、索引4、特征4、quality_contract14、quality_rubric1不变。

- [三阶段设计](screening_pipeline_technical_design_v4.md)
- [字段字典](screening_downstream_labeled_v2_field_dictionary.md)
- [配置与续跑](screening_pipeline_configuration_v4.md)
- [质量判定标准](screening_quality_judgment_standards.md)
- [P2 自适应采样](screening_v4_p2_adaptive.md)
- [批跑](screening_batch_runner.md)
- [整体冒烟与单模块复测](../../module_tests/README.md)
- [原始下游需求](screening下游所需数据详情记录.md)：参考材料，不随代码改写。

运行位于 <output-root>/<run-id>/internal；交付位于同 run 的 external/evaluation-set-large。取消独立发布目录。旧运行只读保留，不原地迁移；可从兼容 v4 冻结结果新建 Stage2 运行。

2026-11-05单盘索引优化已同步至[技术设计](screening_pipeline_technical_design_v4.md)及[配置、计时日志与参数说明](screening_pipeline_configuration_v4.md)。默认缓存仍为1024 MiB；4096 MiB仅为可选试用值。采样判据、四字段模型标准及上述契约版本未因本轮性能优化改变。
