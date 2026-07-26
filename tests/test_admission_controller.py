import json
import sys
import tempfile
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from admission_controller import run_admission


SOURCE = "urn:li:dataset:(urn:li:dataPlatform:dbt,shop.customers,PROD)"
TARGET = "urn:li:dataset:(urn:li:dataPlatform:dbt,shop.dashboard,PROD)"


class AdmissionControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.context = self.root / "context.json"
        self.gov_policy = self.root / "governance.json"
        self.impact_policy = self.root / "impact.json"
        self.candidate = self.root / "candidate.sql"
        self.repaired = self.root / "repaired.sql"
        self.context.write_text(
            json.dumps(
                {
                    "table_name": "customers",
                    "urn": SOURCE,
                    "fields": [
                        {"fieldPath": "customer_id", "nativeDataType": "INTEGER"},
                        {
                            "fieldPath": "first_name",
                            "nativeDataType": "VARCHAR",
                            "editedTags": ["PII"],
                        },
                    ],
                    "downstream_assets": [
                        {
                            "urn": TARGET,
                            "name": "shop.dashboard",
                            "type": "DATASET",
                            "degree": 1,
                            "criticality": "LOW",
                            "is_representation": False,
                            "lineage_path": [
                                {"urn": SOURCE, "type": "DATASET"},
                                {"urn": TARGET, "type": "DATASET"},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.gov_policy.write_text(
            json.dumps(
                {
                    "policy_id": "pii",
                    "scope": "final_output",
                    "denied_classifications": ["PII"],
                    "fail_closed_on_unresolved_lineage": True,
                    "allowed_actions_by_classification": {"PII": ["exclude"]},
                    "repair": {"strategy": "drop_projection", "max_attempts": 1},
                }
            ),
            encoding="utf-8",
        )
        self.impact_policy.write_text(
            json.dumps(
                {
                    "policy_id": "impact",
                    "ignore_representation_copies": True,
                    "require_verified_paths": True,
                    "default_criticality": "MEDIUM",
                    "no_downstream_action": "AUTO_PR",
                    "actions": {
                        "LOW": "AUTO_PR",
                        "MEDIUM": "REVIEW_REQUIRED",
                        "HIGH": "BLOCK_AUTO_MERGE",
                    },
                }
            ),
            encoding="utf-8",
        )
        self.candidate.write_text(
            "select customer_id, first_name from {{ ref('customers') }}\n",
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def run_with(self, repair_function):
        return run_admission(
            sql_path=self.candidate,
            context_path=self.context,
            governance_policy_path=self.gov_policy,
            impact_policy_path=self.impact_policy,
            repair_output=self.repaired,
            task="Return customer identifiers and names.",
            repair_function=repair_function,
        )

    def test_one_repair_receives_structured_report_and_passes(self):
        calls = []

        def repair(sql, report, context, task):
            calls.append(report)
            self.assertEqual(report["gate"], "governance")
            self.assertEqual(report["status"], "BLOCK")
            self.assertEqual(report["violations"][0]["allowed_actions"], ["exclude"])
            return "select customer_id from {{ ref('customers') }}"

        result = self.run_with(repair)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["repair"]["attempts"], 1)
        self.assertEqual(result["repair"]["status"], "PASS")
        self.assertEqual(result["admission_action"], "AUTO_PR")

    def test_failed_repair_is_not_retried(self):
        calls = []

        def repair(sql, report, context, task):
            calls.append(report)
            return sql

        result = self.run_with(repair)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["repair"]["attempts"], 1)
        self.assertEqual(result["repair"]["status"], "FAIL")
        self.assertEqual(result["admission_action"], "BLOCK_AUTO_MERGE")

    def test_repair_provider_error_blocks_without_retry(self):
        calls = []

        def repair(sql, report, context, task):
            calls.append(report)
            raise TimeoutError("provider unavailable")

        result = self.run_with(repair)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["repair"]["attempts"], 1)
        self.assertEqual(result["repair"]["status"], "ERROR")
        self.assertEqual(result["admission_action"], "BLOCK_AUTO_MERGE")


if __name__ == "__main__":
    unittest.main()
