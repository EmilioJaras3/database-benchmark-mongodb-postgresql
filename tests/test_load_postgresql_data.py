import unittest
from unittest import mock

import load_postgresql_data as loader


class PostgreSQLLoaderTests(unittest.TestCase):
    def test_profile_maps_only_to_benchmark_postgres_service(self):
        self.assertEqual(
            loader.service_for_profile("PROFILE_A_BASELINE"), "postgres-profile-a"
        )
        self.assertEqual(
            loader.service_for_profile("PROFILE_B_MEDIUM"), "postgres-profile-b"
        )
        self.assertEqual(
            loader.service_for_profile("PROFILE_C_SEVERE"), "postgres-profile-c"
        )
        with self.assertRaises(loader.LoaderError):
            loader.service_for_profile("tc-db")

    def test_container_must_have_exact_compose_project_and_service_labels(self):
        result = mock.Mock(returncode=0, stdout="container-id\n")
        inspect = mock.Mock(returncode=0, stdout="tc-project|postgres\n")
        with mock.patch.object(loader.subprocess, "run", side_effect=[result, inspect]) as run:
            with self.assertRaises(loader.LoaderError):
                loader.resolve_container("PROFILE_A_BASELINE")
        self.assertEqual(run.call_count, 2)

    def test_existing_public_orders_table_is_refused_before_write_process(self):
        query = mock.Mock(returncode=0, stdout="t\n")
        with mock.patch.object(loader.subprocess, "run", return_value=query) as run, mock.patch.object(
            loader.subprocess, "Popen"
        ) as popen:
            with self.assertRaises(loader.LoaderError):
                loader.ensure_target_is_empty("benchmark-container")
        self.assertIn("SELECT to_regclass('public.orders') IS NOT NULL;", run.call_args.args[0])
        popen.assert_not_called()

    def test_copy_script_has_transactional_exact_count_guard_and_timestamp_index(self):
        script = loader.copy_script() + loader.copy_script_suffix()
        self.assertIn("BEGIN;", script)
        self.assertIn("ROLLBACK;", script)
        self.assertIn("COUNT(*) = 500000", script)
        self.assertIn(
            'USING btree ((data->>\'timestamp\' COLLATE "C"))', script
        )
        self.assertNotIn("DROP TABLE", script.upper())
        self.assertNotIn("TRUNCATE", script.upper())

    def test_copy_terminator_has_no_extra_blank_row(self):
        self.assertTrue(loader.copy_script_suffix().startswith("\\.\n"))

    def test_psql_stops_on_sql_errors(self):
        self.assertIn("--set=ON_ERROR_STOP=1", loader.psql_command("safe-container"))

    def test_snapshot_text_wrapper_detaches_without_closing_underlying_snapshot(self):
        import tempfile

        snapshot = tempfile.TemporaryFile(mode="w+b")
        try:
            snapshot.write(b"validated dataset")
            snapshot.seek(0)
            observed = loader.with_snapshot_text(snapshot, lambda text: text.read())

            self.assertEqual("validated dataset", observed)
            snapshot.seek(0)
            self.assertEqual(b"validated dataset", snapshot.read())
        finally:
            snapshot.close()


if __name__ == "__main__":
    unittest.main()
