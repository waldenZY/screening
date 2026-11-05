"""Read-only validation and import of immutable Stage1 evidence."""
from pathlib import Path
import json
import shutil

from ..shared.json_io import load_jsonl, sha256_file, stable_hash, write_json
from ..delivery.screened import validate_screened_canonical_records
from ..quality.runner import QUALITY_CONTRACT_VERSION, admission_task_key, admission_input_sha256
from ..candidate.contract import validate_canonical_candidate
from ..quality.annotation import validate_quality_annotation, quality_annotation_input
from .checkpoints import CheckpointStore, _record_count
from .orchestrator import _branch_contract, parse_build_config


def read_object(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"元数据必须是对象: {path}")
    return value


def inspect_source(path: Path):
    """Never create files in the origin, including when validation fails."""
    from ..delivery.validation import (validate_manifest_source, _task_maps,
        _validate_admission_components, _validate_admission_binding, _validate_challenge_binding)
    root = path.expanduser().resolve()
    if (root / "internal/manifest.json").is_file():
        root = root / "internal"
    manifest = read_object(root / "manifest.json")
    if manifest.get("pipeline_version") not in {9, 10} or manifest.get("contract", {}).get("quality_contract_version") != QUALITY_CONTRACT_VERSION:
        raise ValueError("--stage1-from 仅支持当前改造前 v4 和新版完整 Stage1，不支持 v3")
    modern = manifest["pipeline_version"] == 10
    snapshots = root / "snapshots" if modern else root
    prompts = snapshots / "prompts"
    quality_contract = read_object(snapshots / "quality_contract.json" if modern else root / "quality/quality_run_contract.json")
    stage1_contract_path = snapshots / "stage1_quality_contract.json"
    stage1_contract = read_object(stage1_contract_path) if stage1_contract_path.exists() else quality_contract
    # Check recorded evidence hashes when the origin has a completed artifact manifest.
    quality_metadata = manifest.get("artifacts", {}).get("quality", {})
    for filename, meta in quality_metadata.items():
        if filename.startswith(("admission", "challenge")) and meta.get("sha256") != sha256_file(root / "quality" / filename):
            raise ValueError("Stage1 来源任务证据已变化")
    from ..shared.risk_keywords import validate_risk_snapshot
    risk_snapshot = read_object(snapshots / "risk_keywords.json")
    validate_risk_snapshot(risk_snapshot)
    if (risk_snapshot["source_sha256"] != manifest["contract"]["risk_keywords_source_sha256"]
            or risk_snapshot["rules_sha256"] != manifest["contract"]["risk_keywords_rules_sha256"]):
        raise ValueError("Stage1 来源风险词快照不一致")
    validate_manifest_source(manifest)
    if sha256_file(Path(manifest["taxonomy"]["path"])) != manifest["taxonomy"]["sha256"]:
        raise ValueError("Stage1 来源 taxonomy 已变化")
    from ..candidate.index import CanonicalIndexHandle
    index_meta = read_object(Path(manifest["index"]["metadata_path"]))
    handle = CanonicalIndexHandle(Path(manifest["index"]["cache_dir"]), Path(manifest["index"]["database_path"]), Path(manifest["index"]["metadata_path"]), index_meta)
    build = parse_build_config(manifest["contract"]["build_config"])
    idf = read_object(snapshots / "dedup_idf.json")
    from ..candidate.dedup import validate_idf_snapshot
    validate_idf_snapshot(idf, handle)
    for component, name in (("admission_features", "admission"), ("admission_category", "category"), ("challenge", "challenge")):
        text = (prompts / f"{name}.md").read_text(encoding="utf-8")
        if stable_hash(text) != stage1_contract["prompt_hashes"][component]:
            raise ValueError("Stage1 来源 Prompt 不一致")
    tasks, challenges, results, challenge_results = _task_maps(root)
    _validate_admission_components(root / "quality", stage1_contract, tasks, results, prompt_root=prompts)
    payloads = {}
    for name in ("P0", "P1", "P2"):
        frozen_path = root / name / "stage1.frozen.jsonl"
        records = load_jsonl(frozen_path)
        validate_screened_canonical_records(name, records)
        freeze_meta = manifest.get("frozen", {}).get(name) if modern else read_object(root / name / "stage1_freeze_report.json")
        if freeze_meta != {"stage": "stage1", "records": len(records), "sha256": sha256_file(frozen_path)}:
            raise ValueError("Stage1 来源冻结文件哈希不一致")
        envelope = read_object(root / name / "checkpoints/stage1_frozen.json")
        payload = envelope["payload"]
        if (envelope.get("checkpoint_schema_version") != 1
                or envelope.get("contract_sha256") != stable_hash(_branch_contract(name, handle, build, idf))
                or envelope.get("payload_sha256") != stable_hash(payload)
                or envelope.get("record_count") != _record_count(payload)
                or payload["sha256"] != freeze_meta["sha256"] or payload["records"] != records
                or [r["candidate"] for r in records] != payload["finalize_inputs"]["selected"]):
            raise ValueError("Stage1 来源恢复检查点不一致")
        expected_run = manifest.get("stage1_origin_run_id", manifest["run_id"])
        for record in records:
            if record["provenance"]["run_id"] != expected_run:
                raise ValueError("Stage1 来源 run 身份不一致")
            _validate_admission_binding(record, tasks, results, stage1_contract)
            if name == "P2":
                _validate_challenge_binding(record, challenges, challenge_results, stage1_contract)
        # Check all observations used for constructing reports and resuming, not just final members.
        for candidate in payload["finalize_inputs"]["evaluated"]:
            identity = candidate["source_identity_sha256"]
            observed = payload["finalize_inputs"]["admission"][identity]
            validate_canonical_candidate(candidate)
            key = admission_task_key(candidate, prompt_sha256=stage1_contract["prompt_hashes"]["admission"], options_sha256=stage1_contract["options_sha256"])
            task = tasks.get(key, {})
            if (observed != results.get(key) or identity not in task.get("source_identities", [])
                    or task.get("input_sha256") != admission_input_sha256(candidate)):
                raise ValueError("Stage1 恢复观察缺少任务依据")
        payloads[name] = payload
    effective = read_object(snapshots / "config.json").get("effective_config", {}) if modern else manifest.get("effective_config", {})
    return {"root": root, "snapshots": snapshots, "prompts": prompts, "manifest": manifest,
            "quality_contract": quality_contract, "stage1_contract": stage1_contract,
            "payloads": payloads, "build": build, "effective": effective, "risk_snapshot": risk_snapshot}


def source_overrides(origin):
    manifest = origin["manifest"]
    risk = origin["effective"].get("inputs", {}).get("risk_keywords")
    if not risk:
        raise ValueError("Stage1 来源缺少风险词表输入记录")
    return {"source": manifest["source"]["path"], "taxonomy": manifest["taxonomy"]["path"], "risk_keywords": risk,
            "admission_prompt": origin["prompts"] / "admission.md", "category_prompt": origin["prompts"] / "category.md",
            "challenge_prompt": origin["prompts"] / "challenge.md",
            "cache_root": Path(manifest["index"]["cache_dir"]).parent}


def import_stage1(origin, context, index, build):
    from .run_context import update_manifest
    destination = context.run_root
    source = origin["root"]
    for stage in ("admission", "admission_features", "admission_category", "challenge"):
        for suffix in ("tasks", "results", "attempts", "cache_hits"):
            target = destination / "quality" / f"{stage}_{suffix}.jsonl"
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(source / "quality" / target.name, target)
    write_json(destination / "snapshots/stage1_quality_contract.json", origin["stage1_contract"])
    for name, payload in origin["payloads"].items():
        folder = destination / name
        folder.mkdir(exist_ok=True)
        for filename in ("stage1.frozen.jsonl", "candidate_tasks.jsonl", "dedup_audit.jsonl"):
            shutil.copyfile(source / name / filename, folder / filename)
        for filename in ("supplement_summary.json", "adaptive_summary.json"):
            if (source / name / filename).is_file():
                shutil.copyfile(source / name / filename, folder / filename)
        # Keep Stage1 history; a completed source branch is not a completed new delivery.
        checkpoints = CheckpointStore(folder, _branch_contract(name, index, build,
            read_object(destination / "snapshots/dedup_idf.json")))
        for path in (source / name / "checkpoints").glob("*.json"):
            if path.stem != "branch_complete":
                checkpoints.save(path.stem, read_object(path)["payload"])
        checkpoints.save("stage1_frozen", payload)
        update_manifest(destination, frozen={name: {"stage": "stage1", "records": len(payload["records"]), "sha256": payload["sha256"]}})
    manifest = read_object(destination / "manifest.json")
    for name in ("P0", "P1", "P2"):
        timing = origin["manifest"].get("branches", {}).get(name, {})
        manifest["branches"][name].update({k: timing[k] for k in ("stage1_started_at", "stage1_elapsed_seconds") if k in timing})
    manifest["stage1_origin_run_id"] = origin["manifest"].get("stage1_origin_run_id", origin["manifest"]["run_id"])
    manifest["stage1_source"] = {"path": str(source), "run_id": origin["manifest"]["run_id"],
                                "manifest_sha256": sha256_file(source / "manifest.json"),
                                "contract": origin["manifest"]["contract"],
                                "quality_contract_sha256": stable_hash(origin["stage1_contract"])}
    write_json(destination / "manifest.json", manifest)


def restore_stage1_identity(context):
    """Imported evidence is already local; do not require a writable origin."""
    if not (context.run_root / "snapshots/stage1_quality_contract.json").is_file():
        raise ValueError("复用 Stage1 的独立契约缺失")


def reuse_annotations(origin, runner):
    """Only validated results with exactly the same actual payload key are imported."""
    contract = origin["quality_contract"]
    if (contract.get("quality_contract_version") != QUALITY_CONTRACT_VERSION
            or contract.get("prompt_hashes", {}).get("quality_annotation") != runner.prompt_hashes["quality_annotation"]
            or contract.get("options_sha256") != runner.options_hash):
        return
    source = origin["root"] / "quality"
    for filename in ("quality_annotation_tasks.jsonl", "quality_annotation_results.jsonl"):
        meta = origin["manifest"].get("artifacts", {}).get("quality", {}).get(filename)
        if meta and sha256_file(source / filename) != meta["sha256"]:
            raise ValueError("来源四字段标注账本已变化")
    task_file, result_file = source / "quality_annotation_tasks.jsonl", source / "quality_annotation_results.jsonl"
    if not task_file.exists() or not result_file.exists():
        return
    tasks = {r["cache_key"]: r for r in load_jsonl(task_file)}
    results = {r["cache_key"]: r["result"] for r in load_jsonl(result_file)}
    if len(tasks) != len(load_jsonl(task_file)) or len(results) != len(load_jsonl(result_file)):
        raise ValueError("来源标注任务或结果重复")
    values = {}
    for payload in origin["payloads"].values():
        runner.bind_admission(payload["finalize_inputs"]["admission"])
        for record in payload["records"]:
            candidate = record["candidate"]
            key = runner.task_id("quality_annotation", candidate)
            if key not in tasks or key not in results:
                continue
            task = tasks[key]
            expected_input = quality_annotation_input(candidate, {"admission": record["quality"]["admission"]})
            if (task.get("stage") != "quality_annotation" or task.get("input_sha256") != stable_hash(expected_input)
                    or task.get("prompt_sha256") != runner.prompt_hashes["quality_annotation"]
                    or task.get("options_sha256") != runner.options_hash
                    or candidate["source_identity_sha256"] not in task.get("source_identities", [])):
                raise ValueError("来源标注与实际输入不一致")
            values[key] = validate_quality_annotation(results[key])
    runner._save_cached("quality_annotation", values)
