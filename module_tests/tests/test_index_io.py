"""Index results and recovery remain identical with deferred query indexes."""
from contextlib import closing
import json
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from screening.candidate import index as module
from screening.candidate.contract import compute_fixed_features, parse_conversation, normalize_source_signals
from screening.candidate.dedup import load_or_build_idf_snapshot
from screening.shared.json_io import sha256_file
from screening.shared.taxonomy import load_taxonomy_mappings


class IndexIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "cluster.jsonl"
        self.taxonomy = self.root / "taxonomy.json"
        self.taxonomy.write_text(json.dumps({"categories": [{"name": "设备", "total_size": 30,
            "assigned_clusters": [1, 2], "sub_intents": [
                {"name": "控制", "size": 20, "cluster_ids": [1]},
                {"name": "播放", "size": 10, "cluster_ids": [2]}]}]}, ensure_ascii=False), encoding="utf-8")
        self.mappings = load_taxonomy_mappings(self.taxonomy)
        self.rows = [{"session": f"s{n}", "text": f"打开客厅第{n}盏灯", "cluster": 1 if n < 8 else 2,
                      "cluster_prob": (0.95, 0.5, 0.2)[n % 3], "frequency": frequency}
                     for n, frequency in enumerate((0, 1, 1, 2, 4, 4, 9, 20, 1, 1, 1))]
        self.rows += [dict(self.rows[1]), {"session": "missing_freq", "text": "播放音乐", "cluster": 2},
                      {"session": "empty", "text": "", "cluster": 1, "frequency": 100},
                      {"session": None, "text": "打开灯", "cluster": 1, "frequency": 200},
                      {"session": "noise", "text": "噪声", "cluster": -1, "frequency": 300}]
        self.write_rows()

    def write_rows(self):
        self.source.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.rows), encoding="utf-8")

    def build(self, cache="cache", logger=None):
        return module.ensure_canonical_candidate_index(self.source, self.mappings,
            taxonomy_sha256=sha256_file(self.taxonomy), cache_root=self.root / cache,
            batch_size=2, progress_every=2, cache_mb=1, min_free_bytes=0, reserve_free_bytes=0, logger=logger)

    def database_rows(self, handle):
        with closing(sqlite3.connect(handle.database_path)) as db:
            return db.execute("SELECT * FROM candidates ORDER BY source_identity_sha256").fetchall()

    def test_full_source_frequency_and_duplicate_semantics_are_unchanged(self):
        handle = self.build()
        with closing(sqlite3.connect(handle.database_path)) as db:
            names = {r[1] for r in db.execute("PRAGMA index_list(candidates)")}
            self.assertTrue({"candidates_l1_source", "candidates_l2_source", "candidates_cluster_source"} <= names)
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"candidates", "build_state"})
            db.row_factory = sqlite3.Row
            candidates = db.execute("SELECT * FROM candidates").fetchall()
        self.assertEqual(len(candidates), 12)
        for candidate in candidates:
            original = self.rows[candidate["source_line"] - 1]
            frequencies = [r.get("frequency", 1) for r in self.rows if r["cluster"] == original["cluster"]]
            frequency = original.get("frequency", 1)
            stats = {"total_count": len(frequencies), "less_count": sum(v < frequency for v in frequencies),
                     "min_frequency": min(frequencies), "max_frequency": max(frequencies)}
            expected = compute_fixed_features(normalize_source_signals(original)[0], parse_conversation(original["text"]), stats)
            self.assertEqual(candidate["frequency_less_count"], stats["less_count"])
            self.assertEqual(candidate["cluster_frequency_count"], stats["total_count"])
            for key in ("probability_band", "frequency_band", "normalized_frequency", "length_band", "complexity", "formality"):
                self.assertEqual(candidate[key], expected[key])
        self.assertEqual(handle.metadata["counts"]["duplicate_rows"], 1)
        self.assertEqual(handle.metadata["counts"]["isolated_rows"], 2)

    def test_scan_interruption_without_query_indexes_and_legacy_resume(self):
        class StopAtProgress(logging.Logger):
            def info(self, msg, *args, **kwargs):
                if msg.startswith("canonical index rows="):
                    raise RuntimeError("synthetic interruption after durable checkpoint")
        with self.assertRaisesRegex(RuntimeError, "synthetic interruption"):
            self.build(logger=StopAtProgress("stop"))
        building = next((self.root / "cache").rglob(module.BUILDING_DATABASE_NAME))
        with closing(sqlite3.connect(building)) as db:
            self.assertEqual([r for r in db.execute("PRAGMA index_list(candidates)") if r[3] == "c"], [])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0], 2)
            # Simulate the indexes retained in an older interrupted v4 build.
            db.execute("CREATE INDEX candidates_l1_source ON candidates(l1,source_line)")
            db.execute("CREATE INDEX candidates_l2_source ON candidates(l2_key,source_line)")
            db.execute("CREATE INDEX candidates_cluster_source ON candidates(cluster_json,source_line)")
            db.commit()
        resumed, fresh = self.build(), self.build("fresh")
        self.assertEqual(self.database_rows(resumed), self.database_rows(fresh))
        self.assertEqual(resumed.metadata["population"], fresh.metadata["population"])
        self.assertEqual(resumed.metadata["counts"], fresh.metadata["counts"])
        digest = sha256_file(resumed.database_path)
        self.build()
        self.assertEqual(sha256_file(resumed.database_path), digest)

    def test_finalization_interruption_recovers_without_parsing_source_again(self):
        with patch.object(module, "_create_query_indexes", side_effect=RuntimeError("stop finalization")):
            with self.assertRaisesRegex(RuntimeError, "stop finalization"):
                self.build()
        with patch.object(module, "parse_conversation", side_effect=AssertionError("must resume past source scan")):
            resumed = self.build()
        fresh = self.build("fresh")
        self.assertEqual(self.database_rows(resumed), self.database_rows(fresh))
        self.assertEqual(resumed.metadata["population"], fresh.metadata["population"])

    def test_identity_collision_is_still_a_fatal_error(self):
        self.rows = [self.rows[0], {**self.rows[0], "cluster": 2}]
        self.write_rows()
        with self.assertRaisesRegex(ValueError, "source_identity_collision"):
            self.build()
        audit = json.loads(next((self.root / "cache").rglob(module.INPUT_AUDIT_NAME)).read_text(encoding="utf-8"))
        self.assertEqual(audit["status"], "fatal")

    def test_stage_timing_and_idf_progress_do_not_change_snapshot(self):
        with self.assertLogs("screening.candidate.index", level="INFO") as logs:
            handle = self.build()
        messages = "\n".join(logs.output)
        for key in ("interval_throughput=", "rows_per_second=", "read_seconds=", "sqlite_seconds=",
                    "checkpoint_seconds=", "other_seconds=", "canonical query index complete", "canonical feature apply complete"):
            self.assertIn(key, messages)
        with self.assertLogs("screening.candidate.dedup", level="INFO") as logs:
            snapshot = load_or_build_idf_snapshot(handle, self.source)
        self.assertIn("canonical dedup IDF complete", "\n".join(logs.output))
        self.assertEqual(snapshot["document_count"], handle.metadata["counts"]["candidate_rows"])
        self.assertEqual(snapshot, load_or_build_idf_snapshot(handle, self.source))


if __name__ == "__main__":
    unittest.main()
