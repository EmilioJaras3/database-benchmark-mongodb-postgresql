import unittest
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import csv

import check_spec


class RunBenchmarksScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = Path(__file__).resolve().parents[1] / "run_benchmarks.sh"

    def test_runner_invocation_uses_absolute_script_directory(self):
        content = self.script.read_text(encoding="utf-8")

        self.assertIn('python3 "$SCRIPT_DIR/benchmark_runner.py"', content)
        self.assertNotIn('python3 benchmark_runner.py', content)

    def test_profile_mapping_and_readiness_gate_are_explicit(self):
        content = self.script.read_text(encoding="utf-8")

        for mapping in (
            "PROFILE_A_BASELINE) suffix=a",
            "PROFILE_B_MEDIUM) suffix=b",
            "PROFILE_C_SEVERE) suffix=c",
            'mongo_service="mongo-profile-$suffix"',
            'postgres_service="postgres-profile-$suffix"',
        ):
            self.assertIn(mapping, content)
        self.assertNotIn("tc-db", content)
        self.assertNotIn("Type READY", content)
        self.assertLess(content.index("  check_profile_readiness\n"), content.index('python3 "$SCRIPT_DIR/benchmark_runner.py"'))
        self.assertIn('mongosh --quiet --eval', content)
        self.assertIn('orders.countDocuments({})', content)
        self.assertIn('SELECT COUNT(*) FROM public.orders', content)
        self.assertIn('query=up{job=\\"$job\\",instance=\\"$target\\"}', content)
        self.assertIn('check_prometheus_target cadvisor cadvisor:8080', content)
        self.assertIn('check_prometheus_target node-exporter node-exporter:9100', content)

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "POSIX bash is required for fake-command integration tests")
    def test_profile_readiness_checks_exact_services_before_scenarios(self):
        result, calls, scenarios = self._run_with_fakes()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(scenarios, 36)
        for suffix in "abc":
            self.assertIn(f"exec -T mongo-profile-{suffix}", calls)
            self.assertIn(f"exec -T postgres-profile-{suffix}", calls)
        self.assertNotIn("tc-db", calls)

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "POSIX bash is required for fake-command integration tests")
    def test_bad_count_or_target_fails_before_any_scenario_without_secrets(self):
        for failure in ("mongo_count", "postgres_count", "cadvisor", "node_exporter"):
            with self.subTest(failure=failure):
                secret = "do-not-print-this-secret"
                result, calls, scenarios = self._run_with_fakes(failure=failure, secret=secret)

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(scenarios, 0)
                self.assertIn("Readiness check failed", result.stderr)
                self.assertNotIn(secret, result.stdout + result.stderr)
                self.assertNotIn("READY", result.stdout + result.stderr)
                self.assertNotIn("tc-db", calls)

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "POSIX bash is required for fake-command integration tests")
    def test_transient_service_and_monitoring_startup_failures_are_retried(self):
        expected_calls = {
            "mongo_startup": "compose exec -T mongo-profile-a",
            "postgres_startup": "compose exec -T postgres-profile-a",
            "cadvisor_startup": 'query=up{job="cadvisor",instance="cadvisor:8080"}',
            "node_exporter_startup": 'query=up{job="node-exporter",instance="node-exporter:9100"}',
        }

        for transient, expected_call in expected_calls.items():
            with self.subTest(transient=transient):
                result, calls, scenarios = self._run_with_fakes(transient=transient)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(scenarios, 36)
                self.assertGreaterEqual(calls.count(expected_call), 2)

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "POSIX bash is required for fake-command integration tests")
    def test_resume_preflights_existing_csv_and_passes_resume_to_every_runner_call(self):
        result, calls, scenarios = self._run_with_fakes(resume=True)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Resume preflight passed: preserving 1 valid existing rows.", result.stdout)
        runner_calls = [line for line in calls.splitlines() if line.startswith("runner ")]
        self.assertEqual(len(runner_calls), 36)
        self.assertTrue(all("--resume" in line for line in runner_calls))
        self.assertEqual(scenarios, 36)

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "POSIX bash is required for fake-command integration tests")
    def test_resume_rejects_invalid_partial_csv_before_services_or_scenarios(self):
        result, calls, scenarios = self._run_with_fakes(resume=True, invalid_resume=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("partial CSV validation failed", result.stderr)
        self.assertEqual(calls, "")
        self.assertEqual(scenarios, 0)

    def _run_with_fakes(self, failure=None, secret="test-secret", transient=None, resume=False,
                        invalid_resume=False):
        bash = shutil.which("bash")
        real_python = shutil.which("python3")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            calls_path = root / "calls.log"
            scenarios_path = root / "scenarios.log"
            csv_path = root / "results.csv"
            if resume:
                row = {
                    "timestamp_epoch": "1800000000", "engine": "mongodb",
                    "profile": "PROFILE_A_BASELINE", "query_id": "Q1", "concurrency": "1",
                    "run_number": "1", "throughput_qps": "", "latency_p50_ms": "",
                    "latency_p95_ms": "", "latency_p99_ms": "", "cpu_throttled_seconds": "",
                    "max_rss_mb": "", "disk_spill": "", "status": "error",
                }
                if invalid_resume:
                    row["engine"] = "unsupported"
                with csv_path.open("w", newline="", encoding="utf-8") as stream:
                    writer = csv.DictWriter(stream, fieldnames=check_spec.CSV_FIELDS, lineterminator="\n")
                    writer.writeheader()
                    writer.writerow(row)
            docker = bin_dir / "docker"
            docker.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$*\" >> \"$CALLS_PATH\"\n"
                "if [[ \"$1 $2 $3 $4\" == 'compose ps --status running' ]]; then exit 0; fi\n"
                "if [[ \"$1 $2\" == 'compose exec' ]]; then\n"
                "  service=$4\n"
                "  marker=\n"
                "  if [[ ${READINESS_TRANSIENT_FAILURE:-} == mongo_startup && $service == mongo-profile-a ]]; then marker=$MONGO_STARTUP_MARKER; fi\n"
                "  if [[ ${READINESS_TRANSIENT_FAILURE:-} == postgres_startup && $service == postgres-profile-a ]]; then marker=$POSTGRES_STARTUP_MARKER; fi\n"
                "  if [[ -n $marker && ! -e $marker ]]; then : > \"$marker\"; exit 1; fi\n"
                "  if [[ $service == mongo-* ]]; then\n"
                "    [[ ${READINESS_FAILURE:-} == mongo_count && $service == mongo-profile-a ]] && echo 499999 || echo 500000\n"
                "  else\n"
                "    [[ ${READINESS_FAILURE:-} == postgres_count && $service == postgres-profile-a ]] && echo 499999 || echo 500000\n"
                "  fi\n"
                "fi\n"
                "exit 0\n",
                encoding="utf-8",
            )
            curl = bin_dir / "curl"
            curl.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'curl %s\\n' \"$*\" >> \"$CALLS_PATH\"\n"
                "query=\n"
                "for arg in \"$@\"; do [[ $arg == query=* ]] && query=${arg#query=}; done\n"
                "marker=\n"
                "if [[ $query == *cadvisor* && ${READINESS_FAILURE:-} == cadvisor ]]; then value=0; else value=1; fi\n"
                "if [[ $query == *node-exporter* && ${READINESS_FAILURE:-} == node_exporter ]]; then value=0; fi\n"
                "if [[ $query == *cadvisor* && ${READINESS_TRANSIENT_FAILURE:-} == cadvisor_startup ]]; then marker=$CADVISOR_STARTUP_MARKER; fi\n"
                "if [[ $query == *node-exporter* && ${READINESS_TRANSIENT_FAILURE:-} == node_exporter_startup ]]; then marker=$NODE_EXPORTER_STARTUP_MARKER; fi\n"
                "if [[ -n $marker && ! -e $marker ]]; then : > \"$marker\"; value=0; fi\n"
                "printf '{\"status\":\"success\",\"data\":{\"resultType\":\"vector\",\"result\":[{\"value\":[0,\"%s\"]}]}}' \"$value\"\n",
                encoding="utf-8",
            )
            python = bin_dir / "python3"
            python.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $1 == \"$BENCHMARK_SCRIPT\" ]]; then printf x >> \"$SCENARIOS_PATH\"; printf 'runner %s\\n' \"$*\" >> \"$CALLS_PATH\"; exit 0; fi\n"
                "exec \"$REAL_PYTHON\" \"$@\"\n",
                encoding="utf-8",
            )
            sleep = bin_dir / "sleep"
            sleep.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
            for executable in (docker, curl, python, sleep):
                executable.chmod(0o755)
            environment = os.environ.copy()
            def shell_path(path):
                match = re.match(r"^([A-Za-z]):[\\/](.*)$", str(path))
                if not match:
                    return str(path).replace("\\", "/")
                return f"/{match.group(1).lower()}/{match.group(2).replace(chr(92), '/') }"

            environment.update({
                "PATH": f"{shell_path(bin_dir)}:/usr/bin:/bin:/c/Python313:/c/Program Files/Git/usr/bin",
                "CALLS_PATH": shell_path(calls_path),
                "SCENARIOS_PATH": shell_path(scenarios_path),
                "BENCHMARK_SCRIPT": shell_path(self.script.parent / "benchmark_runner.py"),
                "REAL_PYTHON": real_python,
                "PROMETHEUS_URL": "http://127.0.0.1:9091",
                "POSTGRES_PASSWORD": secret,
                "MONGO_STARTUP_MARKER": shell_path(root / "mongo-startup.marker"),
                "POSTGRES_STARTUP_MARKER": shell_path(root / "postgres-startup.marker"),
                "CADVISOR_STARTUP_MARKER": shell_path(root / "cadvisor-startup.marker"),
                "NODE_EXPORTER_STARTUP_MARKER": shell_path(root / "node-exporter-startup.marker"),
            })
            if failure:
                environment["READINESS_FAILURE"] = failure
            if transient:
                environment["READINESS_TRANSIENT_FAILURE"] = transient
            script_command = [bash, shell_path(self.script)]
            if resume:
                script_command.append("--resume")
            script_command.append(shell_path(csv_path))
            result = subprocess.run(
                script_command,
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                timeout=30,
            )
            calls = calls_path.read_text(encoding="utf-8") if calls_path.exists() else ""
            scenarios = len(scenarios_path.read_text(encoding="utf-8")) if scenarios_path.exists() else 0
            return result, calls, scenarios


if __name__ == "__main__":
    unittest.main()
