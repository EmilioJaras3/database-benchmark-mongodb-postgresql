import csv
import hashlib
import tempfile
import unittest
from pathlib import Path

import check_spec


class PartialCsvValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path = Path(self.temp_dir.name) / "partial.csv"

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def row(run_number="1"):
        return {
            "timestamp_epoch": "1800000000",
            "engine": "mongodb",
            "profile": "PROFILE_A_BASELINE",
            "query_id": "Q1",
            "concurrency": "1",
            "run_number": run_number,
            "throughput_qps": "1.0",
            "latency_p50_ms": "10.0",
            "latency_p95_ms": "20.0",
            "latency_p99_ms": "30.0",
            "cpu_throttled_seconds": "0.0",
            "max_rss_mb": "10.0",
            "disk_spill": "false",
            "status": "ok",
        }

    def write_rows(self, rows):
        with self.path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=check_spec.CSV_FIELDS, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    def test_partial_validator_accepts_valid_incomplete_rows_and_returns_keys(self):
        self.write_rows([self.row("1"), self.row("2")])

        errors, row_count, keys = check_spec.validate_partial_csv(self.path)

        self.assertEqual(errors, [])
        self.assertEqual(row_count, 2)
        self.assertEqual(keys, {
            ("mongodb", "PROFILE_A_BASELINE", "Q1", "1", "1"),
            ("mongodb", "PROFILE_A_BASELINE", "Q1", "1", "2"),
        })

    def test_partial_validator_rejects_duplicate_or_unsupported_keys(self):
        row = self.row()
        unsupported = self.row("2")
        unsupported["engine"] = "unknown"
        self.write_rows([row, row.copy(), unsupported])

        errors, row_count, _ = check_spec.validate_partial_csv(self.path)

        self.assertEqual(row_count, 3)
        self.assertTrue(any("duplicate matrix/repetition key" in error for error in errors))
        self.assertTrue(any("engine has an unsupported value" in error for error in errors))

    def test_partial_validator_rejects_malformed_metrics(self):
        row = self.row()
        row["latency_p99_ms"] = "not-a-number"
        self.write_rows([row])

        errors, row_count, _ = check_spec.validate_partial_csv(self.path)

        self.assertEqual(row_count, 1)
        self.assertTrue(any("latency_p99_ms must be numeric or blank" in error for error in errors))

    def test_partial_validation_never_changes_the_input_hash(self):
        row = self.row()
        row["engine"] = "unsupported"
        self.write_rows([row])
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()

        check_spec.validate_partial_csv(self.path)

        after = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.assertEqual(after, before)

    def test_existing_full_validator_still_rejects_incomplete_matrix(self):
        self.write_rows([self.row()])

        errors, row_count = check_spec.validate_csv(self.path)

        self.assertEqual(row_count, 1)
        self.assertTrue(any("expected exactly 108 data rows" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
