import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import duckdb


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator import (
    PROMPT_CONTRACT_VERSION,
    SYSTEM_PROMPT_TEMPLATE,
    build_fair_system_prompt,
    run,
    seal_run,
    sha256_bytes,
    verify_run,
)


class OrchestratorTests(unittest.TestCase):
    def test_prompt_pair_changes_only_datahub_context_block(self):
        context = {
            "table_name": "customers",
            "urn": "urn:li:dataset:test",
            "fields": [{"fieldPath": "customer_id"}],
            "upstream_tables": [],
        }
        blind = build_fair_system_prompt(context=None, table_name="customers")
        grounded = build_fair_system_prompt(context=context, table_name="customers")
        prefix = "<DATAHUB_CONTEXT>\n"
        suffix = "\n</DATAHUB_CONTEXT>"
        blind_before, blind_rest = blind.split(prefix, maxsplit=1)
        _blind_block, blind_after = blind_rest.split(suffix, maxsplit=1)
        grounded_before, grounded_rest = grounded.split(prefix, maxsplit=1)
        _grounded_block, grounded_after = grounded_rest.split(suffix, maxsplit=1)
        self.assertEqual(blind_before, grounded_before)
        self.assertEqual(blind_after, grounded_after)
        self.assertEqual(PROMPT_CONTRACT_VERSION, "fair_context_ablation_v1")

    def test_manifest_verifies_then_detects_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            run_dir = Path(temporary) / "run"
            run_dir.mkdir()
            artifact = run_dir / "sql" / "candidate.sql"
            artifact.parent.mkdir()
            artifact.write_text("select 1\n", encoding="utf-8")
            seal_run(
                run_dir=run_dir,
                run_id="test-run",
                created_at="2026-01-01T00:00:00+00:00",
                git={"commit": "abc", "dirty": False, "dirty_path_count": 0},
                task_sha256=sha256_bytes(b"task"),
                prompt_template_sha256=sha256_bytes(SYSTEM_PROMPT_TEMPLATE.encode()),
                blind_prompt_sha256=sha256_bytes(b"blind"),
                grounded_prompt_sha256=sha256_bytes(b"grounded"),
                status="PASS",
            )
            self.assertEqual(verify_run(run_dir)["status"], "PASS")

            artifact.chmod(stat.S_IWRITE | stat.S_IREAD)
            artifact.write_text("select 2\n", encoding="utf-8")
            verification = verify_run(run_dir)
            self.assertEqual(verification["status"], "FAIL")
            self.assertEqual(
                verification["violations"][0]["type"],
                "file_digest_mismatch",
            )

            for path in run_dir.rglob("*"):
                if path.is_file():
                    path.chmod(stat.S_IWRITE | stat.S_IREAD)

    def test_full_bounded_run_is_sealed_after_one_repair(self):
        blind_sql = "select customer_name from {{ ref('customers') }}"
        grounded_sql = """select
    customer_id as customer_key,
    first_name as given_name,
    coalesce(customer_lifetime_value, 0) as lifetime_value,
    ntile(2) over (order by customer_id) as value_quintile
from {{ ref('customers') }}
"""
        repaired_sql = """select
    customer_id as customer_key,
    coalesce(customer_lifetime_value, 0) as lifetime_value,
    ntile(2) over (order by customer_id) as value_quintile
from {{ ref('customers') }}
"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task_path = root / "task.txt"
            context_path = root / "context.json"
            policy_path = root / "policy.json"
            contract_path = root / "contract.json"
            database_path = root / "demo.duckdb"
            task_path.write_text("Segment customers and include names.\n", encoding="utf-8")
            context_path.write_text(
                json.dumps(
                    {
                        "table_name": "customers",
                        "urn": "urn:li:dataset:test",
                        "fields": [
                            {"fieldPath": "customer_id", "nativeDataType": "INTEGER"},
                            {
                                "fieldPath": "first_name",
                                "nativeDataType": "VARCHAR",
                                "editedTags": ["PII"],
                            },
                            {
                                "fieldPath": "customer_lifetime_value",
                                "nativeDataType": "DOUBLE",
                            },
                        ],
                        "upstream_tables": [],
                    }
                ),
                encoding="utf-8",
            )
            policy_path.write_text(
                json.dumps(
                    {
                        "policy_id": "test_pii",
                        "denied_classifications": ["PII"],
                        "fail_closed_on_unresolved_lineage": True,
                        "allowed_actions_by_classification": {"PII": ["exclude"]},
                        "repair": {
                            "strategy": "drop_projection",
                            "replacement_identity": "customer_id",
                            "max_attempts": 1,
                        },
                    }
                ),
                encoding="utf-8",
            )
            contract_path.write_text(
                json.dumps(
                    {
                        "contract_id": "test_result",
                        "allow_extra_columns": True,
                        "required_columns": [
                            "customer_key",
                            "lifetime_value",
                            "value_quintile",
                        ],
                        "exact_row_count": 2,
                        "non_null_columns": [
                            "customer_key",
                            "lifetime_value",
                            "value_quintile",
                        ],
                        "null_defaults": {
                            "lifetime_value": {
                                "value": 0,
                                "expected_replacements": 1,
                            }
                        },
                        "unique_columns": ["customer_key"],
                        "numeric_ranges": {"value_quintile": {"min": 1, "max": 2}},
                        "value_counts": {"value_quintile": {"1": 1, "2": 1}},
                    }
                ),
                encoding="utf-8",
            )
            connection = duckdb.connect(str(database_path))
            connection.execute(
                "create table customers as select * from values "
                "(1, 'Ada', null), (2, 'Grace', 12.5) "
                "t(customer_id, first_name, customer_lifetime_value)"
            )
            connection.close()

            args = SimpleNamespace(
                task_file=task_path,
                context=context_path,
                policy=policy_path,
                contract=contract_path,
                database=database_path,
                runs_root=root / "runs",
                run_id="test-paired-run",
                writeback=False,
                evidence_url=None,
                gms_url="http://unused",
                actor="urn:li:corpuser:test",
                require_clean_git=True,
            )
            try:
                with (
                    patch(
                        "orchestrator.generate_sql",
                        side_effect=[blind_sql, grounded_sql, repaired_sql],
                    ) as generate,
                    patch(
                        "orchestrator.git_snapshot",
                        return_value={
                            "commit": "test-commit",
                            "dirty": False,
                            "dirty_path_count": 0,
                        },
                    ),
                ):
                    report, run_dir = run(args)

                self.assertEqual(generate.call_count, 3)
                self.assertEqual(report["blind"]["gates"]["schema"]["status"], "FAIL")
                self.assertEqual(
                    {
                        key: value["status"]
                        for key, value in report["grounded_initial"]["gates"].items()
                    },
                    {
                        "schema": "PASS",
                        "runtime": "PASS",
                        "result_quality": "PASS",
                        "governance": "FAIL",
                    },
                )
                self.assertTrue(report["final"]["all_gates_pass"])
                self.assertEqual(report["repair"]["attempts"], 1)
                self.assertEqual(report["status"], "PASS")
                self.assertEqual(run_dir, root / "runs" / "test-paired-run")
                self.assertFalse((root / "runs" / ".pending" / "test-paired-run").exists())
                self.assertEqual(verify_run(run_dir)["status"], "PASS")
            finally:
                for path in root.rglob("*"):
                    if path.is_file():
                        path.chmod(stat.S_IWRITE | stat.S_IREAD)


if __name__ == "__main__":
    unittest.main()
