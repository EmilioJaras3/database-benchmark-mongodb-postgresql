import unittest
from pathlib import Path

from benchmark_runner import postgres_dsn_from_env


class PostgreSQLDsnTests(unittest.TestCase):
    def test_default_dsn_uses_benchmark_port_and_encodes_password(self):
        dsn = postgres_dsn_from_env({"POSTGRES_PASSWORD": "p@ss:/? word"})

        self.assertEqual(
            "postgresql://postgres:p%40ss%3A%2F%3F%20word@127.0.0.1:5434/postgres",
            dsn,
        )

    def test_explicit_dsn_takes_precedence(self):
        explicit = "postgresql://custom@example.test:6543/bench"

        self.assertEqual(
            explicit,
            postgres_dsn_from_env(
                {"POSTGRES_DSN": explicit, "POSTGRES_PASSWORD": "must-not-override"}
            ),
        )

    def test_no_dsn_or_password_returns_none(self):
        self.assertIsNone(postgres_dsn_from_env({}))

    def test_all_postgres_profiles_publish_host_5434_to_container_5432(self):
        compose = Path(__file__).resolve().parents[1] / "docker-compose.yml"
        content = compose.read_text(encoding="utf-8")

        self.assertEqual(3, content.count('"127.0.0.1:5434:5432"'))
        self.assertNotIn('"127.0.0.1:5432:5432"', content)


if __name__ == "__main__":
    unittest.main()
