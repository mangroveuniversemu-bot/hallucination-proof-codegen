import sys
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from result_gate import evaluate_result_contract


CONTRACT = {
    "contract_id": "test_contract",
    "allow_extra_columns": True,
    "required_columns": ["customer_key", "lifetime_value", "value_quintile"],
    "exact_row_count": 2,
    "non_null_columns": ["customer_key", "lifetime_value", "value_quintile"],
    "null_defaults": {
        "lifetime_value": {"value": 0, "expected_replacements": 1}
    },
    "unique_columns": ["customer_key"],
    "numeric_ranges": {"value_quintile": {"min": 1, "max": 2}},
    "value_counts": {"value_quintile": {"1": 1, "2": 1}},
}


def execution(rows):
    return {
        "gate": "runtime",
        "status": "PASS",
        "columns": ["customer_key", "lifetime_value", "value_quintile"],
        "rows": rows,
    }


class ResultQualityGateTests(unittest.TestCase):
    def test_valid_result_passes_explicit_null_and_shape_contract(self):
        result = evaluate_result_contract(execution([(1, 0, 1), (2, 12.5, 2)]), CONTRACT)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["null_quality"]["lifetime_value"]["null_count"], 0)
        self.assertEqual(
            result["null_quality"]["lifetime_value"]["default_replacement_status"],
            "PASS",
        )
        self.assertEqual(result["uniqueness"]["customer_key"]["duplicate_count"], 0)

    def test_unexpected_null_reports_declared_default(self):
        result = evaluate_result_contract(execution([(1, None, 1), (2, 12.5, 2)]), CONTRACT)
        self.assertEqual(result["status"], "FAIL")
        violation = next(
            item for item in result["violations"] if item["type"] == "unexpected_nulls"
        )
        self.assertEqual(violation["column"], "lifetime_value")
        self.assertEqual(violation["null_count"], 1)
        self.assertEqual(violation["declared_default"], 0)

    def test_wrong_non_null_default_is_rejected(self):
        result = evaluate_result_contract(execution([(1, -1, 1), (2, 12.5, 2)]), CONTRACT)
        self.assertEqual(result["status"], "FAIL")
        violation = next(
            item
            for item in result["violations"]
            if item["type"] == "null_default_count_mismatch"
        )
        self.assertEqual(violation["expected_replacements"], 1)
        self.assertEqual(violation["actual_replacements"], 0)

    def test_uniqueness_range_and_bucket_counts_are_enforced(self):
        result = evaluate_result_contract(execution([(1, 0, 1), (1, 12.5, 3)]), CONTRACT)
        self.assertEqual(result["status"], "FAIL")
        types = {item["type"] for item in result["violations"]}
        self.assertIn("uniqueness_violation", types)
        self.assertIn("numeric_range_violation", types)
        self.assertIn("value_count_mismatch", types)

    def test_runtime_failure_skips_result_gate(self):
        result = evaluate_result_contract(
            {"gate": "runtime", "status": "FAIL", "message": "binder error"},
            CONTRACT,
        )
        self.assertEqual(result["status"], "SKIPPED")


if __name__ == "__main__":
    unittest.main()
