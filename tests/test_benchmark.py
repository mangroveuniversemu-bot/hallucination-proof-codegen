import sys
import tempfile
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from benchmark import (
    DEFAULT_CONFIG,
    DEFAULT_POLICY,
    compare_results,
    evaluate_candidate,
    load_benchmark,
    load_policy,
)


class BenchmarkTests(unittest.TestCase):
    def test_result_gate_checks_columns_order_and_values(self):
        expected = {
            "status": "PASS",
            "columns": ["segment", "customer_count"],
            "rows": [("HIGH", 2), ("LOW", 3)],
        }
        self.assertEqual(compare_results(expected, expected)["status"], "PASS")

        wrong_columns = {**expected, "columns": ["segment", "row_count"]}
        result = compare_results(wrong_columns, expected)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["issues"][0]["type"], "column_contract_mismatch")

        wrong_order = {**expected, "rows": list(reversed(expected["rows"]))}
        result = compare_results(wrong_order, expected)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["issues"][0]["type"], "row_value_mismatch")

    def test_all_reference_queries_pass_all_four_gates(self):
        policy = load_policy(DEFAULT_POLICY)
        _, worlds = load_benchmark(DEFAULT_CONFIG)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for world in worlds:
                for task in world["tasks"]:
                    sql_path = root / f"{task['id']}.sql"
                    sql_path.write_text(task["reference_sql"], encoding="utf-8")
                    result = evaluate_candidate(world, task, sql_path, policy)
                    self.assertTrue(task["id"], result["verified_merge_ready"])
                    self.assertEqual(
                        {gate["status"] for gate in result["gates"].values()},
                        {"PASS"},
                    )


if __name__ == "__main__":
    unittest.main()
