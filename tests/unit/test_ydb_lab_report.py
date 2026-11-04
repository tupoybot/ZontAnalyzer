from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.ydb_lab_report import _memoized_report, _metric_lines, compact_record, summarize_output


def operation(variant: str, years: int, repeat: int, *, scenario="lookup", phase="operation",
              suite="application", measured_ru=4.0, result_hash="a" * 64):
    return {
        "kind": "operation", "variant": variant, "suite": suite, "source_sha": "f" * 40,
        "years": years, "repeat": repeat, "scenario": scenario, "phase": phase,
        "elapsed_seconds": repeat * 0.5, "observation": {
            "all_rpc_complete": True, "measurement_complete": measured_ru is not None,
            "measured_ru": measured_ru, "ru_present_rpc_count": int(measured_ru is not None),
            "ru_missing_rpc_count": int(measured_ru is None), "failed_rpc_count": 0,
            "estimated_ru": None, "stats_complete": True, "cpu_complete": True,
            "io_complete": True,
            "metrics": {"total_cpu_us": 500_000, "read_bytes": 128,
                        "update_bytes": 32, "delete_rows": 0},
        },
        "events": [{"billed": True, "measured_ru": measured_ru,
                    "started_seconds": 0.1, "completed_seconds": 0.2,
                    "rpc": "ExecuteQuery", "category": "data", "status": 400000,
                    "grpc_status": "StatusCode.OK", "complete": True, "failed": False,
                    "cpu_complete": True, "io_complete": True,
                    "estimated_ru": None,
                    "stats_present": True, "result_rows": 1,
                    "metrics": {"compile_cpu_us": 0, "process_cpu_us": 0,
                                "phase_cpu_us": 0, "total_cpu_us": 500_000,
                                "read_rows": 1, "read_bytes": 128,
                                "update_rows": 1, "update_bytes": 32,
                                "delete_rows": 0}}],
        "result_sha256": result_hash, "queue_depth": 2, "queue_age_seconds": 3.5,
        "expected_error": False,
    }


def validation(variant: str, suite: str, years: int, repeat: int, **hashes):
    return {"kind": "validation", "variant": variant, "suite": suite,
            "years": years, "repeat": repeat,
            "facts_sha256": hashes.get("facts_sha256", "1" * 64),
            "artifacts_sha256": hashes.get("artifacts_sha256", "2" * 64),
            "ai_sha256": hashes.get("ai_sha256", "3" * 64)}


def complete_rows(repeats=3):
    rows = []
    for years in (1, 5, 10):
        for repeat in range(1, repeats + 1):
            for variant in ("baseline", "candidate"):
                for suite in ("gas", "application"):
                    rows.append(operation(variant, years, repeat, suite=suite))
                    rows.append(validation(variant, suite, years, repeat))
                    if suite == "application":
                        for observed, elapsed, cpu in ((False, 1.0, 0.001), (True, 1.2, 0.003)):
                            rows.append({"kind": "overhead", "variant": variant, "suite": suite,
                                         "years": years, "repeat": repeat, "observed": observed,
                                         "elapsed_seconds": elapsed, "cpu_seconds": cpu, "queries": 5})
    return rows


class YdbLabReportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = Path(self.directory.name)
        self.path = self.output / "results.jsonl"

    def tearDown(self):
        self.directory.cleanup()

    def write_rows(self, rows, trailing=""):
        self.path.write_text("".join(json.dumps(row) + "\n" for row in rows) + trailing,
                             encoding="utf-8")

    def test_complete_repeated_parity_writes_summary_and_csvs(self):
        self.write_rows(complete_rows())
        summary = summarize_output(self.output)
        self.assertTrue(summary["validation_passed"])
        self.assertNotIn("accepted", summary)
        self.assertTrue(summary["hash_parity"])
        self.assertEqual(summary["repeat_counts"], {1: 3, 5: 3, 10: 3})
        self.assertTrue((self.output / "operations.csv").is_file())
        self.assertTrue((self.output / "windows.csv").is_file())
        with (self.output / "operations.csv").open(encoding="utf-8", newline="") as stream:
            self.assertIn("duration_seconds_median", next(csv.reader(stream)))
        self.assertEqual({row["width_seconds"] for row in summary["windows"]}, {1, 60})
        self.assertGreater(len(summary["windows"]), 3 * 3 * 2 * 2 * 2)
        operation_group = next(row for row in summary["operations"]
                               if row["variant"] == "baseline" and row["years"] == 1
                               and row["suite"] == "application" and row["phase"] == "operation")
        self.assertEqual(operation_group["duration_seconds"],
                         {"median": 1.0, "min": 0.5, "max": 1.5})
        self.assertEqual(operation_group["measured_ru"]["median"], 4.0)
        self.assertEqual(operation_group["update_bytes"]["median"], 32.0)
        self.assertEqual(operation_group["delete_rows"]["median"], 0.0)
        overhead = next(row for row in summary["overhead"] if row["variant"] == "baseline" and
                        row["suite"] == "application" and row["years"] == 1)
        for value in overhead["wall_overhead_seconds"].values():
            self.assertAlmostEqual(value, 0.2)
        for value in overhead["cpu_overhead_seconds"].values():
            self.assertAlmostEqual(value, 0.002)

    def test_hash_mismatch_and_unpaired_variant_raise(self):
        rows = complete_rows()
        candidate_validation = next(index for index, row in enumerate(rows)
                                    if row.get("kind") == "validation" and
                                    row["variant"] == "candidate" and row["suite"] == "gas" and
                                    row["years"] == 1 and row["repeat"] == 1)
        rows[candidate_validation]["facts_sha256"] = "9" * 64
        self.write_rows(rows)
        with self.assertRaisesRegex(ValueError, "facts_sha256 mismatch"):
            summarize_output(self.output)
        rows = [row for row in complete_rows() if not (
            row["kind"] == "operation" and row["variant"] == "candidate" and
            row["suite"] == "gas" and row["years"] == 1 and row["repeat"] == 1)]
        self.write_rows(rows)
        with self.assertRaisesRegex(ValueError, "missing operation variant"):
            summarize_output(self.output)

    def test_validation_hashes_remain_stable_across_repeats(self):
        rows = complete_rows()
        for row in rows:
            if (row.get("kind") == "validation" and row["suite"] == "gas" and
                    row["years"] == 1 and row["repeat"] == 2):
                row["artifacts_sha256"] = "9" * 64
        self.write_rows(rows)
        with self.assertRaisesRegex(ValueError, "changed across variants or repeats"):
            summarize_output(self.output)

    def test_duplicate_operation_record_is_rejected(self):
        rows = complete_rows()
        duplicate = next(row for row in rows if row.get("kind") == "operation" and
                         row["variant"] == "baseline" and row["suite"] == "gas" and
                         row["years"] == 1 and row["repeat"] == 1)
        rows.append(dict(duplicate))
        self.write_rows(rows)
        with self.assertRaisesRegex(ValueError, "duplicate operation record"):
            summarize_output(self.output)

    def test_source_revision_is_stable_across_repeats_per_variant(self):
        rows = complete_rows()
        target = next(row for row in rows if row.get("kind") == "operation" and
                      row["variant"] == "candidate" and row["suite"] == "application" and
                      row["years"] == 1 and row["repeat"] == 2)
        target["source_sha"] = "e" * 40
        self.write_rows(rows)
        with self.assertRaisesRegex(ValueError, "source_sha changed across repeats"):
            summarize_output(self.output)

    def test_quick_run_is_not_accepted_and_missing_billable_ru_stays_null(self):
        rows = complete_rows(repeats=2)
        rows[0] = operation("baseline", 1, 1, suite="gas", measured_ru=None)
        self.write_rows(rows)
        summary = summarize_output(self.output)
        self.assertFalse(summary["validation_passed"])
        group = next(row for row in summary["operations"]
                     if row["variant"] == "baseline" and row["years"] == 1 and
                     row["suite"] == "gas")
        self.assertIsNone(group["measured_ru"]["median"])
        self.assertFalse(group["complete_accounting"])

    def test_expected_failure_attempt_with_accounted_ru_does_not_break_acceptance(self):
        rows = complete_rows()
        target = next(row for row in rows if row.get("kind") == "operation" and
                      row["variant"] == "candidate" and row["suite"] == "application" and
                      row["years"] == 1 and row["repeat"] == 1)
        target["expected_error"] = True
        target["observation"]["failed_rpc_count"] = 1
        self.write_rows(rows)
        self.assertTrue(summarize_output(self.output)["validation_passed"])

    def test_incomplete_nonbillable_control_does_not_hide_complete_billed_accounting(self):
        rows = complete_rows()
        target = next(row for row in rows if row.get("kind") == "operation" and
                      row["variant"] == "candidate" and row["suite"] == "application" and
                      row["years"] == 1 and row["repeat"] == 1)
        target["observation"]["all_rpc_complete"] = False
        target["observation"]["all_billed_rpc_complete"] = True
        self.write_rows(rows)
        self.assertTrue(summarize_output(self.output)["validation_passed"])

    def test_missing_ru_keeps_matrix_validation_separate_from_accounting(self):
        rows = complete_rows()
        for row in rows:
            if (row.get("kind") == "operation" and row["variant"] == "candidate" and
                    row["suite"] == "application" and row["years"] == 1):
                row["observation"]["estimated_ru"] = 50.0
        target = next(row for row in rows if row.get("kind") == "operation" and
                      row["variant"] == "candidate" and row["suite"] == "application" and
                      row["years"] == 1 and row["repeat"] == 1)
        target["observation"]["measured_ru"] = None
        target["observation"]["measurement_complete"] = False
        target["observation"]["ru_missing_rpc_count"] = 1
        target["events"][0]["measured_ru"] = None
        self.write_rows(rows)
        summary = summarize_output(self.output)
        self.assertTrue(summary["validation_passed"])
        self.assertFalse(summary["accounting_complete"])
        self.assertIn("missing_ru", summary["accounting_incomplete_reasons"])
        total = next(row for row in summary["operation_totals"]
                     if row["variant"] == "candidate" and row["suite"] == "application" and
                     row["years"] == 1)
        self.assertIsNone(total["measured_ru_total"]["median"])
        self.assertEqual(total["partial_known_ru_total"]["median"], 4.0)
        self.assertEqual(total["estimated_ru_total"]["median"], 50.0)

    def test_incomplete_final_jsonl_record_is_ignored_but_middle_corruption_fails(self):
        self.write_rows(complete_rows(), trailing='{"kind":"operation"')
        self.assertTrue(summarize_output(self.output)["validation_passed"])
        self.path.write_text('{"kind":\n' + json.dumps(complete_rows()[0]) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Malformed JSONL"):
            summarize_output(self.output)
        self.path.write_text(json.dumps(complete_rows()[0]) + "\n{\"kind\":\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Malformed JSONL"):
            summarize_output(self.output)

    def test_metrics_have_bounded_labels_and_no_ids_or_hashes(self):
        self.write_rows(complete_rows())
        summary = summarize_output(self.output)
        metrics = _metric_lines(summary)
        expected = ('zont_lab_ru_per_second{variant="baseline",years="1",suite="application",'
                    'scenario="lookup",window="1"}')
        self.assertIn(expected, metrics)
        self.assertIn('zont_lab_ru_coverage_ratio', metrics)
        self.assertNotIn("source_sha", metrics)
        self.assertNotIn("result_sha256", metrics)
        self.assertNotIn("repeat=", metrics)
        self.assertNotIn("queue_age_seconds=", metrics)
        self.assertIn("zont_lab_update_bytes", metrics)
        self.assertIn("zont_lab_delete_rows", metrics)
        self.assertNotIn("zont_lab_write_bytes", metrics)

    def test_scenario_label_cardinality_is_capped(self):
        rows = []
        for index in range(140):
            scenario = f"case_{index:02d}"
            rows.extend([operation("baseline", 1, 3, scenario=scenario),
                         operation("candidate", 1, 3, scenario=scenario)])
        self.write_rows(rows)
        summary = summarize_output(self.output)
        metrics = _metric_lines(summary)
        scenarios = {line.split('scenario="', 1)[1].split('"', 1)[0]
                     for line in metrics.splitlines() if 'scenario="' in line}
        self.assertEqual(len(scenarios), 128)
        self.assertTrue(summary["scenario_labels_truncated"])
        self.assertIn("zont_lab_scenario_labels_truncated 1", metrics)

    def test_live_report_is_incomplete_without_mutating_output(self):
        self.write_rows([operation("baseline", 1, 1)])
        summary = summarize_output(self.output, write=False, strict=False)
        self.assertFalse(summary["validation_passed"])
        self.assertFalse((self.output / "summary.json").exists())
        self.assertFalse((self.output / "operations.csv").exists())

    def test_raw_and_compact_summaries_match_and_compact_path_skips_raw(self):
        rows = complete_rows()
        self.write_rows(rows)
        with patch.object(Path, "read_text", side_effect=AssertionError("whole-file read")):
            raw_summary = summarize_output(self.output, write=False)
        raw_metrics = _metric_lines(raw_summary)
        compact_rows = [compact_record(row) for row in rows]
        self.assertTrue(all("events" not in row for row in compact_rows
                            if row.get("kind") == "operation"))
        compact_path = self.output / "measurements.jsonl"
        compact_path.write_text("".join(json.dumps(row) + "\n" for row in compact_rows),
                                encoding="utf-8")
        self.path.write_text("this raw file must not be opened\n", encoding="utf-8")
        compact_summary = summarize_output(self.output, write=False)
        self.assertEqual(compact_summary, raw_summary)
        self.assertEqual(_metric_lines(compact_summary), raw_metrics)

    def test_compacted_missing_ru_keeps_incomplete_flags_and_rpc_rate(self):
        row = operation("candidate", 1, 1, suite="application", measured_ru=None)
        compact = compact_record(row)
        self.assertNotIn("events", compact)
        self.write_rows([compact])
        summary = summarize_output(self.output, write=False, strict=False)
        self.assertFalse(summary["accounting_complete"])
        self.assertEqual(summary["rpc_coverage"]["billed_rpc_count"], 1)
        self.assertEqual(summary["rpc_coverage"]["ru_missing_rpc_count"], 1)
        self.assertIn("missing_ru", summary["accounting_incomplete_reasons"])
        metrics = _metric_lines(summary)
        self.assertIn("zont_lab_rpc_per_second", metrics)
        self.assertNotIn("zont_lab_ru_total{", metrics)

    def test_rpc_coverage_includes_setup_rows(self):
        rows = complete_rows()
        for variant in ("baseline", "candidate"):
            rows.append(operation(variant, 1, 1, suite="application",
                                  scenario="schema_setup", phase="setup"))
        self.write_rows(rows)
        summary = summarize_output(self.output, write=False)
        self.assertEqual(summary["rpc_coverage"]["rpc_count"], 38)
        self.assertEqual(summary["rpc_coverage"]["billed_rpc_count"], 38)
        self.assertEqual(summary["rpc_coverage"]["stats_present_rpc_count"], 38)

    def test_unchanged_input_reuses_report_until_file_signature_changes(self):
        self.write_rows(complete_rows())
        loader = _memoized_report(self.output)
        with patch("tools.ydb_lab_report.summarize_output", wraps=summarize_output) as summarize:
            first = loader()
            second = loader()
            self.assertIs(first[0], second[0])
            self.assertEqual(summarize.call_count, 1)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write("\n")
            third = loader()
            self.assertEqual(summarize.call_count, 2)
            self.assertEqual(first, third)


if __name__ == "__main__":
    unittest.main()
