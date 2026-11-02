import json
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

import benchmark_runner


class BenchmarkRunnerMetricsTests(unittest.TestCase):
    def test_mongodb_log_filter_uses_operation_profiling_filter_document(self):
        config = (benchmark_runner.ROOT / "docker" / "mongodb-benchmark.yml").read_text(
            encoding="utf-8"
        )

        self.assertNotIn("systemLog:\n  filter", config)
        self.assertIn(
            "operationProfiling:\n  mode: off\n"
            "  filter: '{ \"command.comment\": { \"$regex\": \"^bench-\" } }'",
            config,
        )

    @staticmethod
    def mongo_log_line(query_id, used_disk=None, message="Slow query"):
        command = {"aggregate": "orders", "comment": query_id}
        attr = {"command": command}
        if used_disk is not None:
            attr["usedDisk"] = used_disk
        entry = {"msg": message, "attr": attr}
        return f"mongo-profile-a  | {json.dumps(entry)}"

    def run_spill_check(self, lines, expected_query_ids=("run-marker-1", "run-marker-2"),
                        side_effect=None, returncode=0):
        result = mock.Mock(returncode=returncode, stdout="\n".join(lines))
        run = mock.patch.object(benchmark_runner.subprocess, "run", return_value=result)
        if side_effect is not None:
            run = mock.patch.object(benchmark_runner.subprocess, "run", side_effect=side_effect)
        with run as subprocess_run:
            spill = benchmark_runner.mongodb_disk_spill(
                "mongo-profile-a", list(expected_query_ids), 1_700_000_000.0
            )
        return spill, subprocess_run

    def test_mongodb_spill_returns_true_for_completed_event_with_used_disk(self):
        line = self.mongo_log_line("run-marker-1", used_disk=True)

        spill, _ = self.run_spill_check([line])

        self.assertIs(spill, True)

    def test_mongodb_spill_returns_false_only_when_every_expected_id_is_completed(self):
        lines = [self.mongo_log_line(query_id) for query_id in ("run-marker-1", "run-marker-2")]

        spill, _ = self.run_spill_check(lines)

        self.assertIs(spill, False)

    def test_mongodb_spill_is_unknown_when_an_expected_id_is_missing(self):
        line = self.mongo_log_line("run-marker-1")

        spill, _ = self.run_spill_check([line])

        self.assertIs(spill, None)

    def test_mongodb_spill_returns_unknown_without_matching_completed_event(self):
        unrelated = self.mongo_log_line("other-run")

        spill, _ = self.run_spill_check([unrelated])

        self.assertIs(spill, None)

    def test_mongodb_spill_keeps_in_progress_only_evidence_unknown(self):
        line = self.mongo_log_line("run-marker-1", message="Slow in-progress query")

        spill, _ = self.run_spill_check([line])

        self.assertIs(spill, None)

    def test_malformed_matching_entry_keeps_negative_evidence_unknown(self):
        completed_without_spill = self.mongo_log_line("run-marker-1")

        spill, _ = self.run_spill_check([completed_without_spill, "truncated run-marker-2 {"])

        self.assertIs(spill, None)

    def test_mongodb_spill_remains_unknown_when_positive_evidence_is_malformed(self):
        completed_with_spill = self.mongo_log_line("run-marker-1", used_disk=True)

        spill, _ = self.run_spill_check([completed_with_spill, "truncated run-marker-2 {"])

        self.assertIs(spill, None)

    def test_mongodb_spill_is_unknown_on_compose_log_failure_or_timeout(self):
        spill, _ = self.run_spill_check([], returncode=1)
        self.assertIs(spill, None)
        spill, _ = self.run_spill_check([], side_effect=benchmark_runner.subprocess.TimeoutExpired("docker compose logs", 30))
        self.assertIs(spill, None)

    def test_mongodb_spill_calls_compose_logs_since_measurement_start(self):
        line = self.mongo_log_line("run-marker-1", used_disk=True)

        spill, subprocess_run = self.run_spill_check([line])

        self.assertIs(spill, True)
        command = subprocess_run.call_args.args[0]
        self.assertEqual(command[:4], ["docker", "compose", "logs", "--no-color"])
        self.assertEqual(command[4:6], ["--since", "2023-11-14T22:13:20+00:00"])
        self.assertEqual(command[-1], "mongo-profile-a")

    def test_resolves_one_full_compose_container_id_for_service(self):
        container_id = "a" * 64
        with mock.patch.object(benchmark_runner.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout=f"{container_id}\n", stderr="")

            resolved = benchmark_runner.resolve_compose_container_id("mongo-profile-a")

        self.assertEqual(resolved, container_id)
        self.assertEqual(run.call_args.args[0], ["docker", "compose", "ps", "--quiet", "mongo-profile-a"])
        self.assertEqual(run.call_args.kwargs["cwd"], benchmark_runner.ROOT)

    def test_refuses_ambiguous_compose_service_identity(self):
        with mock.patch.object(benchmark_runner.subprocess, "run") as run:
            run.return_value = mock.Mock(
                returncode=0, stdout=f"{'a' * 64}\n{'b' * 64}\n", stderr=""
            )

            resolved = benchmark_runner.resolve_compose_container_id("postgres-profile-b")

        self.assertIsNone(resolved)

    def test_queries_exact_container_and_uses_metric_name_without_bytes_suffix(self):
        container_id = "a" * 64
        payload = {
            "status": "success",
            "data": {
                "result": [{
                    "metric": {"id": f"/docker/{container_id}"},
                    "values": [[1, "12"], [2, "18"]],
                }]
            },
        }
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with mock.patch.object(benchmark_runner.urllib.request, "urlopen", return_value=response) as urlopen:
            samples = benchmark_runner.query_prometheus_range(
                "http://prometheus:9090", "container_memory_rss", container_id, 1, 2
            )

        query = parse_qs(urlparse(urlopen.call_args.args[0].full_url).query)["query"][0]
        self.assertEqual(query, f'container_memory_rss{{id="/docker/{container_id}"}}')
        self.assertEqual(samples, [(1, 12.0), (2, 18.0)])

    def test_rejects_multiple_prometheus_series_for_container(self):
        container_id = "a" * 64
        payload = {
            "status": "success",
            "data": {"result": [
                {"metric": {"id": f"/docker/{container_id}"}, "values": [[1, "12"]]},
                {"metric": {"id": f"/docker/{container_id}"}, "values": [[1, "18"]]},
            ]},
        }
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        with mock.patch.object(benchmark_runner.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(RuntimeError):
                benchmark_runner.query_prometheus_range(
                    "http://prometheus:9090", "container_memory_rss", container_id, 1, 2
                )

    def test_read_telemetry_resolves_target_and_preserves_postgres_service_name(self):
        expected_id = "c" * 64
        with (
            mock.patch.object(benchmark_runner, "resolve_compose_container_id", return_value=expected_id) as resolve,
            mock.patch.object(
                benchmark_runner, "query_prometheus_range",
                side_effect=[[(1, 1.0), (2, 3.0)], [(1, 1048576.0), (2, 2097152.0)]],
            ) as query,
        ):
            cpu, rss = benchmark_runner.read_telemetry(
                "http://prometheus:9090", "postgresql", "PROFILE_B_MEDIUM", 100
            )

        self.assertEqual(resolve.call_args.args, ("postgres-profile-b",))
        self.assertEqual(query.call_args_list[0].args[3:], (100, 400))
        self.assertEqual(query.call_args_list[0].args[2], expected_id)
        self.assertEqual(query.call_args_list[1].args[2], expected_id)
        self.assertEqual((cpu, rss), (2.0, 2.0))

    def test_mongodb_telemetry_uses_mongo_service_and_expected_metric_names(self):
        expected_id = "d" * 64
        with (
            mock.patch.object(benchmark_runner, "resolve_compose_container_id", return_value=expected_id) as resolve,
            mock.patch.object(
                benchmark_runner, "query_prometheus_range",
                side_effect=[[(1, 2.0), (2, 5.0)], [(1, 1048576.0)]],
            ) as query,
        ):
            cpu, rss = benchmark_runner.read_telemetry(
                "http://prometheus:9090", "mongodb", "PROFILE_A_BASELINE", 20
            )

        self.assertEqual(resolve.call_args.args, ("mongo-profile-a",))
        self.assertEqual(query.call_args_list[0].args[:3], (
            "http://prometheus:9090", "container_cpu_cfs_throttled_seconds_total", expected_id
        ))
        self.assertEqual(query.call_args_list[1].args[:3], (
            "http://prometheus:9090", "container_memory_rss", expected_id
        ))
        self.assertEqual((cpu, rss), (3.0, 1.0))

    def test_error_reason_codes_identify_deadline_worker_and_missing_observations(self):
        reasons = benchmark_runner.error_reason_codes(
            worker_reasons=["measurement_deadline_exceeded", "worker_query_failed"],
            missing_worker_results=True,
            forced_shutdown=True,
            unreaped=True,
            spill=None,
            cpu=None,
            rss=12.5,
        )

        self.assertEqual(reasons, (
            "cpu_telemetry_missing",
            "measurement_deadline_exceeded",
            "spill_evidence_unknown",
            "worker_forced_shutdown",
            "worker_query_failed",
            "worker_result_missing",
            "worker_unreaped",
        ))

    def test_error_diagnostic_is_stderr_only_and_does_not_mutate_row_schema(self):
        row = benchmark_runner.failed_row(
            mock.Mock(engine="mongodb", profile="PROFILE_A_BASELINE", query_id="Q1", concurrency=1),
            1,
            "error",
            123,
        )
        original_row = dict(row)
        with mock.patch("sys.stderr") as stderr, mock.patch("sys.stdout") as stdout:
            benchmark_runner.emit_error_diagnostic("error", 1, ["worker_query_failed"])

        self.assertEqual(tuple(row), benchmark_runner.CSV_FIELDS)
        self.assertEqual(row, original_row)
        self.assertEqual(stderr.write.call_args_list, [
            mock.call("run=1 status=error reason_codes=worker_query_failed"),
            mock.call("\n"),
        ])
        stdout.write.assert_not_called()

    def test_non_error_status_emits_no_diagnostic(self):
        with mock.patch("sys.stderr") as stderr:
            benchmark_runner.emit_error_diagnostic("ok", 1, ["worker_query_failed"])

        stderr.write.assert_not_called()

    def test_diagnostic_does_not_change_csv_fields_or_existing_failed_row_values(self):
        args = mock.Mock(
            engine="mongodb", profile="PROFILE_A_BASELINE", query="Q1", concurrency=1
        )
        row = benchmark_runner.failed_row(args, 2, "error", 123.9)

        self.assertEqual(tuple(benchmark_runner.CSV_FIELDS), (
            "timestamp_epoch", "engine", "profile", "query_id", "concurrency", "run_number",
            "throughput_qps", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms",
            "cpu_throttled_seconds", "max_rss_mb", "disk_spill", "status",
        ))
        self.assertEqual(row, {
            "timestamp_epoch": 123, "engine": "mongodb", "profile": "PROFILE_A_BASELINE",
            "query_id": "Q1", "concurrency": 1, "run_number": 2, "throughput_qps": "",
            "latency_p50_ms": "", "latency_p95_ms": "", "latency_p99_ms": "",
            "cpu_throttled_seconds": "", "max_rss_mb": "", "disk_spill": "", "status": "error",
        })

    def run_repetition_with_unreaped_workers(self, readiness_result):
        context = mock.MagicMock()
        context.Process.return_value = mock.MagicMock()
        args = SimpleNamespace(
            engine="mongodb", profile="PROFILE_A_BASELINE", query="Q1", query_id="Q1",
            concurrency=1,
        )
        stderr = io.StringIO()
        with (
            mock.patch.object(benchmark_runner.multiprocessing, "get_context", return_value=context),
            mock.patch.object(benchmark_runner, "wait_for_ready", side_effect=readiness_result)
            if isinstance(readiness_result, BaseException)
            else mock.patch.object(benchmark_runner, "wait_for_ready", return_value=readiness_result),
            mock.patch.object(benchmark_runner, "stop_workers", return_value=(False, True)) as stop_workers,
            redirect_stderr(stderr),
        ):
            row, _, _, unreaped = benchmark_runner.run_repetition(
                args, 1, "invocation", [], [], None, "mongodb://unused", "http://unused"
            )
        stop_workers.assert_called_once()
        self.assertTrue(unreaped)
        return row, stderr.getvalue()

    def test_readiness_timeout_diagnostic_keeps_timeout_code_when_worker_is_unreaped(self):
        row, output = self.run_repetition_with_unreaped_workers(TimeoutError("private details"))

        self.assertEqual(row["status"], "error")
        self.assertEqual(
            output.strip(),
            "run=1 status=error reason_codes=worker_readiness_timeout,worker_unreaped",
        )
        self.assertNotIn("private details", output)

    def test_startup_error_diagnostic_keeps_startup_code_when_worker_is_unreaped(self):
        ready = [("ready", 0, "error", None, "worker_startup_failed")]

        row, output = self.run_repetition_with_unreaped_workers(ready)

        self.assertEqual(row["status"], "error")
        self.assertEqual(
            output.strip(),
            "run=1 status=error reason_codes=worker_startup_failed,worker_unreaped",
        )

    def run_mongodb_worker_once(self, aggregate_side_effect=None, monotonic_values=(9.0, 11.0),
                                stop_checks=(False,)):
        collection = mock.MagicMock()
        if aggregate_side_effect is not None:
            collection.aggregate.side_effect = aggregate_side_effect
        database = mock.MagicMock()
        database.__getitem__.return_value = collection
        client = mock.MagicMock()
        client.get_default_database.return_value = database
        mongo_client = mock.MagicMock(return_value=client)
        pymongo_module = mock.Mock(MongoClient=mongo_client)
        events = mock.MagicMock()
        deadline = mock.MagicMock(value=10.0)
        stop_event = mock.Mock(is_set=mock.Mock(side_effect=stop_checks))
        with (
            mock.patch.dict(sys.modules, {"pymongo": pymongo_module}),
            mock.patch.object(benchmark_runner.time, "monotonic", side_effect=monotonic_values),
        ):
            benchmark_runner.run_worker(
                0, "mongodb", "mongodb://unused", "Q1", [], [], None, "bench-run",
                mock.Mock(), mock.Mock(is_set=mock.Mock(return_value=True)), stop_event,
                deadline, events,
            )
        return events.put.call_args.args[0], collection

    def test_late_successful_query_is_excluded_without_error_status(self):
        result, collection = self.run_mongodb_worker_once()

        self.assertEqual(collection.aggregate.call_count, 1)
        self.assertEqual(result[:5], ("result", 0, 0, [], "ok"))
        self.assertEqual(result[6], "")
        self.assertEqual(len(result[7]), 1)
        comment = collection.aggregate.call_args.kwargs["comment"]
        self.assertTrue(comment.startswith("bench-run-"))
        self.assertEqual(result[7], [comment])

    def test_measured_queries_receive_distinct_comment_ids(self):
        result, collection = self.run_mongodb_worker_once(
            monotonic_values=(9.0, 9.0, 9.0, 11.0), stop_checks=(False, False)
        )

        comments = [call.kwargs["comment"] for call in collection.aggregate.call_args_list]
        self.assertEqual(len(comments), 2)
        self.assertEqual(len(set(comments)), 2)
        self.assertEqual(result[7], comments)

    def test_repetition_passes_all_measured_query_ids_to_spill_check(self):
        context = mock.MagicMock()
        context.Process.return_value = mock.MagicMock()
        events = context.Queue.return_value
        query_ids = ["bench-run-0-1", "bench-run-0-2"]
        events.get.return_value = ("result", 0, 1, [12.0], "ok", None, "", query_ids)
        args = SimpleNamespace(
            engine="mongodb", profile="PROFILE_B_MEDIUM", query="Q1", query_id="Q1",
            concurrency=1,
        )
        with (
            mock.patch.object(benchmark_runner.multiprocessing, "get_context", return_value=context),
            mock.patch.object(benchmark_runner, "wait_for_ready", return_value=[("ready", 0, "ok", None)]),
            mock.patch.object(benchmark_runner, "stop_workers", return_value=(False, False)),
            mock.patch.object(benchmark_runner, "read_telemetry", return_value=(1.0, 2.0)),
            mock.patch.object(benchmark_runner, "mongodb_disk_spill", return_value=False) as spill_check,
            mock.patch.object(benchmark_runner.time, "sleep"),
            mock.patch.object(benchmark_runner.time, "time", side_effect=[100.0, 200.0]),
            mock.patch.object(benchmark_runner.time, "monotonic", return_value=10.0),
        ):
            row, _, _, unreaped = benchmark_runner.run_repetition(
                args, 1, "invocation", [], [], None, "mongodb://unused", "http://unused"
            )

        self.assertFalse(unreaped)
        self.assertEqual(row["status"], "ok")
        self.assertEqual(spill_check.call_args.args, ("mongo-profile-b", query_ids, 200.0))

    def test_forced_worker_shutdown_does_not_read_a_potentially_corrupt_queue(self):
        context = mock.MagicMock()
        context.Process.return_value = mock.MagicMock()
        events = context.Queue.return_value
        args = SimpleNamespace(
            engine="mongodb", profile="PROFILE_A_BASELINE", query="Q2", query_id="Q2",
            concurrency=1,
        )
        stderr = io.StringIO()
        with (
            mock.patch.object(benchmark_runner.multiprocessing, "get_context", return_value=context),
            mock.patch.object(benchmark_runner, "wait_for_ready", return_value=[("ready", 0, "ok", None)]),
            mock.patch.object(benchmark_runner, "stop_workers", return_value=(True, False)),
            mock.patch.object(benchmark_runner, "read_telemetry", return_value=(None, None)),
            mock.patch.object(benchmark_runner, "mongodb_disk_spill", return_value=None),
            mock.patch.object(benchmark_runner.time, "sleep"),
            mock.patch.object(benchmark_runner.time, "time", side_effect=[100.0, 200.0]),
            mock.patch.object(benchmark_runner.time, "monotonic", return_value=10.0),
            redirect_stderr(stderr),
        ):
            row, _, _, unreaped = benchmark_runner.run_repetition(
                args, 1, "invocation", [], [], None, "mongodb://unused", "http://unused"
            )

        self.assertFalse(unreaped)
        self.assertEqual(row["status"], "error")
        events.get.assert_not_called()
        self.assertIn("worker_forced_shutdown", stderr.getvalue())
        self.assertIn("worker_result_missing", stderr.getvalue())

    def test_resume_skips_every_already_recorded_repetition(self):
        args = SimpleNamespace(
            engine="mongodb", profile="PROFILE_A_BASELINE", query="Q1", concurrency=1,
            csv="existing.csv", resume=True,
        )
        keys = {
            ("mongodb", "PROFILE_A_BASELINE", "Q1", "1", str(run))
            for run in range(1, benchmark_runner.REPETITIONS + 1)
        }
        output = io.StringIO()
        with (
            mock.patch.object(benchmark_runner, "parse_args", return_value=args),
            mock.patch.object(benchmark_runner, "read_mongodb_pipeline", return_value=[]),
            mock.patch.object(benchmark_runner.check_spec, "validate_partial_csv", return_value=([], 3, keys)),
            mock.patch.object(benchmark_runner, "flush_wsl_cache") as flush,
            mock.patch.object(benchmark_runner, "run_repetition") as run,
            mock.patch.object(benchmark_runner, "append_row") as append,
            redirect_stdout(output),
        ):
            benchmark_runner.main()

        flush.assert_not_called()
        run.assert_not_called()
        append.assert_not_called()
        self.assertEqual(output.getvalue().count("already recorded"), 3)

    def test_resume_runs_only_missing_keys_and_preserves_existing_rows(self):
        args = SimpleNamespace(
            engine="mongodb", profile="PROFILE_A_BASELINE", query="Q1", concurrency=1,
            csv="existing.csv", resume=True,
        )
        existing = {
            ("mongodb", "PROFILE_A_BASELINE", "Q1", "1", "1"),
            ("mongodb", "PROFILE_A_BASELINE", "Q1", "1", "3"),
        }
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(benchmark_runner, "parse_args", return_value=args),
            mock.patch.object(benchmark_runner, "read_mongodb_pipeline", return_value=[]),
            mock.patch.object(benchmark_runner.check_spec, "validate_partial_csv", return_value=([], 2, existing)),
            mock.patch.object(benchmark_runner, "flush_wsl_cache") as flush,
            mock.patch.object(benchmark_runner, "run_repetition", return_value=({"status": "ok"}, 10.0, 20.0, False)) as run,
            mock.patch.object(benchmark_runner, "append_row") as append,
            redirect_stdout(io.StringIO()),
        ):
            benchmark_runner.main()

        flush.assert_called_once_with(2)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[1], 2)
        append.assert_called_once()

    def test_resume_fails_closed_on_invalid_partial_csv_before_cache_flush(self):
        args = SimpleNamespace(
            engine="mongodb", profile="PROFILE_A_BASELINE", query="Q1", concurrency=1,
            csv="existing.csv", resume=True,
        )
        with (
            mock.patch.object(benchmark_runner, "parse_args", return_value=args),
            mock.patch.object(benchmark_runner, "read_mongodb_pipeline", return_value=[]),
            mock.patch.object(benchmark_runner.check_spec, "validate_partial_csv", return_value=(["bad row"], 1, set())),
            mock.patch.object(benchmark_runner, "flush_wsl_cache") as flush,
        ):
            with self.assertRaises(SystemExit) as raised:
                benchmark_runner.main()

        self.assertIn("Resume CSV validation failed", str(raised.exception))
        flush.assert_not_called()

    def test_query_failure_remains_error_when_deadline_behavior_changes(self):
        result, _ = self.run_mongodb_worker_once(RuntimeError("secret backend detail"))

        self.assertEqual(result[:5], ("result", 0, 0, [], "error"))
        self.assertEqual(result[6], "worker_query_failed")
        self.assertNotIn("secret backend detail", repr(result))

    def test_mongodb_client_startup_failure_emits_sanitized_readiness_event(self):
        events = mock.MagicMock()
        pymongo_module = mock.Mock(
            MongoClient=mock.Mock(side_effect=RuntimeError("secret backend detail"))
        )

        with mock.patch.dict(sys.modules, {"pymongo": pymongo_module}):
            benchmark_runner.run_worker(
                0, "mongodb", "mongodb://unused", "Q1", [], [], None, "bench-run",
                mock.Mock(), mock.Mock(is_set=mock.Mock(return_value=True)),
                mock.Mock(), mock.Mock(value=10.0), events,
            )

        event = events.put.call_args.args[0]
        self.assertEqual(event, ("ready", 0, "error", None, "worker_startup_failed"))
        self.assertNotIn("secret backend detail", repr(event))

    def test_cache_flush_uses_exact_wsl_command_before_each_repetition(self):
        args = SimpleNamespace(
            engine="mongodb", profile="PROFILE_A_BASELINE", query="Q1", concurrency=1,
            csv="unused.csv",
        )
        completed = mock.Mock(returncode=0, stdout="", stderr="")
        with (
            mock.patch.object(benchmark_runner, "parse_args", return_value=args),
            mock.patch.object(benchmark_runner, "read_mongodb_pipeline", return_value=[]),
            mock.patch.object(benchmark_runner, "run_repetition", return_value=({"status": "ok"}, 1, 2, False)) as run,
            mock.patch.object(benchmark_runner, "append_row"),
            mock.patch.object(benchmark_runner.subprocess, "run", return_value=completed) as subprocess_run,
            mock.patch.dict(benchmark_runner.os.environ, {"WSL_DISTRO_NAME": "Ubuntu"}),
            redirect_stdout(io.StringIO()) as output,
        ):
            benchmark_runner.main()

        self.assertEqual(run.call_count, benchmark_runner.REPETITIONS)
        self.assertEqual(subprocess_run.call_count, benchmark_runner.REPETITIONS)
        self.assertEqual(
            subprocess_run.call_args_list[0].args[0],
            ["wsl.exe", "--distribution", "Ubuntu", "--user", "root", "--exec", "sh", "-c",
             "sync; echo 3 > /proc/sys/vm/drop_caches"],
        )
        self.assertEqual(
            [call.kwargs["timeout"] for call in subprocess_run.call_args_list],
            [30] * benchmark_runner.REPETITIONS,
        )
        self.assertEqual(
            output.getvalue().count("WSL2 cache flush completed before repetition"),
            benchmark_runner.REPETITIONS,
        )

    def test_cache_flush_failure_stops_before_measurement_and_csv_append(self):
        args = SimpleNamespace(
            engine="mongodb", profile="PROFILE_A_BASELINE", query="Q1", concurrency=1,
            csv="unused.csv",
        )
        failed = mock.Mock(returncode=1, stdout="secret output", stderr="secret error")
        with (
            mock.patch.object(benchmark_runner, "parse_args", return_value=args),
            mock.patch.object(benchmark_runner, "read_mongodb_pipeline", return_value=[]),
            mock.patch.object(benchmark_runner, "run_repetition") as run,
            mock.patch.object(benchmark_runner, "append_row") as append,
            mock.patch.object(benchmark_runner.subprocess, "run", return_value=failed),
            mock.patch.dict(benchmark_runner.os.environ, {"WSL_DISTRO_NAME": "PrivateDistro"}),
        ):
            with self.assertRaises(SystemExit) as raised:
                benchmark_runner.main()

        self.assertEqual(str(raised.exception), "WSL2 cache flush failed; stopping before measurement.")
        run.assert_not_called()
        append.assert_not_called()
        self.assertNotIn("PrivateDistro", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))

    def test_cache_flush_fails_closed_for_missing_distro_and_process_errors(self):
        for environment, error in (
            ({}, None),
            ({"WSL_DISTRO_NAME": "PrivateDistro"}, OSError("secret os detail")),
            ({"WSL_DISTRO_NAME": "PrivateDistro"}, benchmark_runner.subprocess.TimeoutExpired("wsl.exe", 30)),
        ):
            with self.subTest(environment=bool(environment), error=type(error).__name__ if error else None):
                with mock.patch.object(benchmark_runner.subprocess, "run", side_effect=error) as subprocess_run:
                    with self.assertRaises(SystemExit) as raised:
                        benchmark_runner.flush_wsl_cache(1, environ=environment)
                if not environment:
                    subprocess_run.assert_not_called()
                self.assertEqual(str(raised.exception), "WSL2 cache flush failed; stopping before measurement.")
                self.assertNotIn("PrivateDistro", str(raised.exception))
                self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
