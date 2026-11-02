import csv
import hashlib
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

import check_spec
import phase4_pipeline


class Phase4PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.csv_path = self.root / "synthetic-matrix.csv"
        self.output_dir = self.root / "synthetic-report"
        self.rows = self.make_rows()
        self.write_rows(self.rows)

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def make_rows():
        rows = []
        for index, key in enumerate(sorted(check_spec.EXPECTED_KEYS)):
            engine, profile, query_id, concurrency, run_number = key
            run = int(run_number)
            rows.append({
                "timestamp_epoch": str(1_800_000_000 + index),
                "engine": engine,
                "profile": profile,
                "query_id": query_id,
                "concurrency": concurrency,
                "run_number": run_number,
                "throughput_qps": str(run),
                "latency_p50_ms": str(run * 10),
                "latency_p95_ms": str(run * 20),
                "latency_p99_ms": str(run * 30),
                "cpu_throttled_seconds": str(run / 10),
                "max_rss_mb": str(100 + run),
                "disk_spill": "false",
                "status": "ok",
            })
        return rows

    def write_rows(self, rows):
        with self.csv_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=check_spec.CSV_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def test_summarize_numbers_ignores_unobserved_values(self):
        self.assertEqual(
            phase4_pipeline.summarize_numbers([1.0, None, 2.0]),
            {"n": 2, "mean": 1.5, "median": 1.5, "minimum": 1.0, "maximum": 2.0},
        )
        self.assertEqual(phase4_pipeline.summarize_numbers([None]), {
            "n": 0, "mean": None, "median": None, "minimum": None, "maximum": None,
        })

    def test_complete_matrix_generates_seven_sections_and_three_svg_charts(self):
        original_hash = hashlib.sha256(self.csv_path.read_bytes()).hexdigest()

        report_path = phase4_pipeline.generate_artifacts(self.csv_path, self.output_dir)

        report = report_path.read_text(encoding="utf-8")
        for heading in (
            "## 1. Especificaciones del host y del entorno Docker",
            "## 2. Método de medición y verificación del baseline",
            "## 3. Resultados consolidados",
            "## 4. Análisis gráfico",
            "## 5. Planes de ejecución",
            "## 6. Interpretación",
            "## 7. Recomendaciones técnicas",
        ):
            self.assertIn(heading, report)
        for chart_name in (
            "p99-vs-concurrency.svg",
            "throttling-vs-qps.svg",
            "rss-vs-concurrency.svg",
        ):
            chart_path = self.output_dir / "figures" / chart_name
            self.assertTrue(chart_path.is_file())
            self.assertEqual(ET.parse(chart_path).getroot().tag, "{http://www.w3.org/2000/svg}svg")
            self.assertIn(chart_name, report)
        self.assertIn(
            "| throughput_qps | queries/s | 1.000 / 2.000 / 3.000 | 3 | 2.000 | 2.000 | 1.000-3.000 |",
            report,
        )
        self.assertEqual(hashlib.sha256(self.csv_path.read_bytes()).hexdigest(), original_hash)

    def test_missing_values_and_non_success_statuses_remain_visible(self):
        target = next(row for row in self.rows if row["profile"] == "PROFILE_A_BASELINE"
                      and row["engine"] == "mongodb" and row["query_id"] == "Q1"
                      and row["concurrency"] == "1" and row["run_number"] == "3")
        target.update({
            "throughput_qps": "", "latency_p50_ms": "", "latency_p95_ms": "",
            "latency_p99_ms": "", "cpu_throttled_seconds": "", "max_rss_mb": "",
            "disk_spill": "", "status": "error",
        })
        self.rows[3]["status"] = "spill"
        self.rows[3]["disk_spill"] = "true"
        self.rows[4].update({
            "throughput_qps": "", "latency_p50_ms": "", "latency_p95_ms": "",
            "latency_p99_ms": "", "cpu_throttled_seconds": "", "max_rss_mb": "",
            "disk_spill": "", "status": "oom",
        })
        self.write_rows(self.rows)
        execution_log = self.write_complete_log()
        with execution_log.open("a", encoding="utf-8") as stream:
            stream.write("mongodb PROFILE_A_BASELINE Q1 concurrency=1 run=3: error\n")
            stream.write("run=3 status=error reason_codes=worker_forced_shutdown,worker_result_missing\n")

        report_path = phase4_pipeline.generate_artifacts(
            self.csv_path, self.output_dir, execution_log=execution_log,
        )

        report = report_path.read_text(encoding="utf-8")
        self.assertIn("error: 1", report)
        self.assertIn("spill: 1", report)
        self.assertIn("oom: 1", report)
        self.assertIn(
            "| throughput_qps | queries/s | 1.000 / 2.000 / not observed | 2 | 1.500 | 1.500 | 1.000-2.000 |",
            report,
        )
        self.assertIn("no capturado", report.lower())
        self.assertIn("worker_forced_shutdown,worker_result_missing", report)

    def test_invalid_matrix_creates_no_report_artifacts(self):
        self.write_rows(self.rows[:-1])

        with self.assertRaises(phase4_pipeline.PipelineError):
            phase4_pipeline.generate_artifacts(self.csv_path, self.output_dir)

        self.assertFalse(self.output_dir.exists())

    def test_malformed_matrix_creates_no_report_artifacts(self):
        self.csv_path.write_text("not,a,valid,matrix\n", encoding="utf-8")

        with self.assertRaises(phase4_pipeline.PipelineError):
            phase4_pipeline.generate_artifacts(self.csv_path, self.output_dir)

        self.assertFalse(self.output_dir.exists())

    def test_existing_output_directory_is_never_overwritten(self):
        self.output_dir.mkdir()
        sentinel = self.output_dir / "keep.txt"
        sentinel.write_text("preserve", encoding="utf-8")

        with self.assertRaises(phase4_pipeline.PipelineError):
            phase4_pipeline.generate_artifacts(self.csv_path, self.output_dir)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "preserve")

    def test_pipeline_does_not_report_when_benchmark_command_fails(self):
        self.csv_path.unlink()

        def fake_runner(*args, **kwargs):
            kwargs["stdout"].write("Benchmark runner failed with exit code 1.\n")
            return mock.Mock(returncode=1)

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(phase4_pipeline.getpass, "getpass", return_value="synthetic-secret") as prompt,
            mock.patch.object(phase4_pipeline.subprocess, "run", side_effect=fake_runner) as run,
        ):
            result = phase4_pipeline.run_pipeline(self.csv_path, self.output_dir)

        self.assertEqual(result, 1)
        self.assertIn("run_benchmarks.sh", run.call_args.args[0][1])
        self.assertNotIn("synthetic-secret", repr(run.call_args.args[0]))
        self.assertNotIn("synthetic-secret", self.csv_path.with_suffix(".execution.log").read_text(encoding="utf-8"))
        prompt.assert_called_once()
        self.assertNotIn("POSTGRES_PASSWORD", os.environ)
        self.assertFalse(self.output_dir.exists())

    def test_pipeline_refuses_existing_csv_output_and_log_before_starting_runner(self):
        with mock.patch.object(phase4_pipeline.subprocess, "run") as run:
            with self.assertRaises(phase4_pipeline.PipelineError):
                phase4_pipeline.run_pipeline(self.csv_path, self.output_dir)
            run.assert_not_called()

            self.csv_path.unlink()
            self.output_dir.mkdir()
            with self.assertRaises(phase4_pipeline.PipelineError):
                phase4_pipeline.run_pipeline(self.csv_path, self.output_dir)
            run.assert_not_called()

            self.output_dir.rmdir()
            log_path = self.root / "existing.log"
            log_path.write_text("preserve", encoding="utf-8")
            with self.assertRaises(phase4_pipeline.PipelineError):
                phase4_pipeline.run_pipeline(self.csv_path, self.output_dir, execution_log=log_path)
            run.assert_not_called()
        self.assertEqual(log_path.read_text(encoding="utf-8"), "preserve")

    def test_wait_mode_waits_for_exact_matrix_process_then_finalizes(self):
        execution_log = self.write_complete_log()
        with (
            mock.patch.object(phase4_pipeline, "wait_for_runner") as wait,
            mock.patch.object(phase4_pipeline.subprocess, "run") as run,
        ):
            result = phase4_pipeline.run_pipeline(
                self.csv_path, self.output_dir, wait_pid=12345, execution_log=execution_log,
            )

        self.assertEqual(result, 0)
        wait.assert_called_once_with(12345, self.csv_path, poll_seconds=30)
        run.assert_not_called()
        self.assertTrue((self.output_dir / "report.md").is_file())

    def test_wait_mode_rejects_incomplete_execution_log_without_report(self):
        execution_log = self.write_complete_log(flushes=107)
        with mock.patch.object(phase4_pipeline, "wait_for_runner"):
            with self.assertRaises(phase4_pipeline.PipelineError):
                phase4_pipeline.run_pipeline(
                    self.csv_path, self.output_dir, wait_pid=12345, execution_log=execution_log,
                )
        self.assertFalse(self.output_dir.exists())

    def test_execution_evidence_allows_flush_from_interrupted_attempt_when_resuming(self):
        execution_log = self.write_complete_log(flushes=109)

        evidence = phase4_pipeline.validate_execution_evidence(execution_log)

        self.assertEqual(evidence["flushes"], 109)

    def test_execution_evidence_rejects_flushes_without_all_completed_scenario_lines(self):
        execution_log = self.write_complete_log(scenarios=107)

        with self.assertRaises(phase4_pipeline.PipelineError):
            phase4_pipeline.validate_execution_evidence(execution_log)

    def test_resume_pipeline_preserves_csv_and_appends_to_existing_execution_log(self):
        original_csv = self.csv_path.read_bytes()
        execution_log = self.write_complete_log(flushes=109)
        original_log = execution_log.read_text(encoding="utf-8")
        completed = mock.Mock(returncode=0)

        with (
            mock.patch.dict(os.environ, {"POSTGRES_PASSWORD": "synthetic-secret"}),
            mock.patch.object(phase4_pipeline.subprocess, "run", return_value=completed) as run,
        ):
            result = phase4_pipeline.run_pipeline(
                self.csv_path, self.output_dir, execution_log=execution_log, resume=True,
            )

        command = run.call_args.args[0]
        self.assertEqual(command[1], str(phase4_pipeline.ROOT / "run_benchmarks.sh"))
        self.assertEqual(command[2:4], ["--resume", str(self.csv_path.resolve())])
        self.assertEqual(self.csv_path.read_bytes(), original_csv)
        self.assertEqual(execution_log.read_text(encoding="utf-8"), original_log)
        self.assertEqual(result, 0)
        self.assertTrue((self.output_dir / "report.md").is_file())

    def test_resume_pipeline_rejects_invalid_partial_csv_before_runner(self):
        self.rows[0]["query_id"] = "QX"
        self.write_rows(self.rows)
        execution_log = self.write_complete_log()

        with mock.patch.object(phase4_pipeline.subprocess, "run") as run:
            with self.assertRaises(phase4_pipeline.PipelineError):
                phase4_pipeline.run_pipeline(
                    self.csv_path, self.output_dir, execution_log=execution_log, resume=True,
                )

        run.assert_not_called()
        self.assertFalse(self.output_dir.exists())

    def test_runner_wait_only_accepts_the_expected_pid_and_matrix_path(self):
        proc_root = self.root / "proc"
        process_dir = proc_root / "12345"
        process_dir.mkdir(parents=True)
        (process_dir / "cmdline").write_bytes(
            b"bash /repo/run_benchmarks.sh results/synthetic-matrix.csv\0"
        )
        (process_dir / "stat").write_text("12345 (bash) S 0 0\n", encoding="utf-8")

        self.assertTrue(phase4_pipeline.runner_process_active(12345, self.csv_path, proc_root))
        with self.assertRaises(phase4_pipeline.PipelineError):
            phase4_pipeline.runner_process_active(12345, self.root / "different.csv", proc_root)
        (process_dir / "stat").write_text("12345 (bash) Z 0 0\n", encoding="utf-8")
        self.assertFalse(phase4_pipeline.runner_process_active(12345, self.csv_path, proc_root))

    def write_complete_log(self, flushes=108, scenarios=108):
        path = self.root / "execution.log"
        lines = [
            f"WSL2 cache flush completed before repetition {index % 3 + 1}/3."
            for index in range(flushes)
        ]
        lines.extend(
            f"{engine} {profile} {query_id} concurrency={concurrency} run={run_number}: ok"
            for engine, profile, query_id, concurrency, run_number
            in sorted(check_spec.EXPECTED_KEYS)[:scenarios]
        )
        lines.extend(
            f"Readiness checks passed for {profile}: MongoDB and PostgreSQL each contain 500000 orders; cAdvisor and Node Exporter are up."
            for profile in check_spec.PROFILES
        )
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
