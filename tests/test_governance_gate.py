import sys
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from governance_gate import evaluate_governance, repair_sql


CONTEXT = {
    "table_name": "customers",
    "fields": [
        {"fieldPath": "customer_id", "nativeDataType": "INTEGER"},
        {
            "fieldPath": "first_name",
            "nativeDataType": "VARCHAR",
            "tags": {
                "tags": [
                    {
                        "tag": {
                            "urn": "urn:li:tag:PII",
                            "properties": {"name": "PII"},
                        }
                    }
                ]
            },
        },
        {
            "fieldPath": "last_name",
            "nativeDataType": "VARCHAR",
            "tags": {
                "tags": [
                    {
                        "tag": {
                            "urn": "urn:li:tag:PII",
                            "properties": {"name": "PII"},
                        }
                    }
                ]
            },
        },
        {"fieldPath": "customer_lifetime_value", "nativeDataType": "DOUBLE"},
    ],
}

POLICY = {
    "policy_id": "pii_direct_projection_v1",
    "scope": "final_output",
    "denied_classifications": ["PII", "urn:li:tag:PII"],
    "fail_closed_on_unresolved_lineage": True,
    "repair": {
        "strategy": "drop_projection",
        "replacement_identity": "customer_id",
        "max_attempts": 1,
    },
}


def evaluate(sql: str):
    return evaluate_governance(sql, CONTEXT, POLICY)


class GovernanceGateTests(unittest.TestCase):
    def test_direct_pii_projection_fails(self):
        result = evaluate(
            "SELECT customer_id, first_name FROM {{ ref('customers') }}"
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(
            {item["source_column"] for item in result["violations"]},
            {"first_name"},
        )

    def test_aliased_pii_projection_fails(self):
        result = evaluate(
            "SELECT first_name AS customer_name FROM {{ ref('customers') }}"
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["violations"][0]["output_column"], "customer_name")

    def test_derived_pii_projection_fails(self):
        result = evaluate(
            "SELECT CONCAT(first_name, ' ', last_name) AS full_name "
            "FROM {{ ref('customers') }}"
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(
            {item["source_column"] for item in result["violations"]},
            {"first_name", "last_name"},
        )

    def test_pii_in_cte_but_not_final_output_passes(self):
        result = evaluate(
            "WITH staged AS ("
            "SELECT customer_id, first_name FROM {{ ref('customers') }}"
            ") SELECT customer_id FROM staged"
        )
        self.assertEqual(result["status"], "PASS")

    def test_count_star_forwarded_through_ctes_passes(self):
        result = evaluate(
            "WITH counted AS ("
            "SELECT customer_id, COUNT(*) AS order_count "
            "FROM {{ ref('customers') }} GROUP BY customer_id"
            "), ranked AS ("
            "SELECT *, ROW_NUMBER() OVER (ORDER BY order_count DESC) AS rn "
            "FROM counted"
            ") SELECT customer_id, order_count FROM ranked WHERE rn <= 2"
        )
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["unresolved_outputs"])

    def test_pii_forwarded_through_cte_still_fails(self):
        result = evaluate(
            "WITH staged AS ("
            "SELECT customer_id, first_name FROM {{ ref('customers') }}"
            ") SELECT first_name FROM staged"
        )
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["violations"][0]["source_column"], "first_name")

    def test_select_star_expands_and_fails(self):
        result = evaluate("SELECT * FROM {{ ref('customers') }}")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(
            {item["source_column"] for item in result["violations"]},
            {"first_name", "last_name"},
        )

    def test_bounded_repair_removes_only_pii_outputs(self):
        sql = (
            "SELECT customer_id, first_name, last_name, customer_lifetime_value "
            "FROM {{ ref('customers') }}"
        )
        original = evaluate(sql)
        repaired_sql, details = repair_sql(sql, original, CONTEXT, POLICY)
        repaired = evaluate(repaired_sql)
        self.assertEqual(repaired["status"], "PASS")
        self.assertEqual(
            set(details["removed_outputs"]),
            {"first_name", "last_name"},
        )
        self.assertIn("customer_id", repaired_sql)
        self.assertNotIn("first_name", repaired_sql)
        self.assertNotIn("last_name", repaired_sql)

    def test_bounded_repair_prunes_unused_pii_from_cte(self):
        sql = (
            "WITH segmented AS (SELECT customer_id, first_name, last_name, "
            "customer_lifetime_value FROM {{ ref('customers') }}) "
            "SELECT customer_id, first_name, last_name, customer_lifetime_value "
            "FROM segmented"
        )
        original = evaluate(sql)
        repaired_sql, details = repair_sql(sql, original, CONTEXT, POLICY)
        repaired = evaluate(repaired_sql)
        self.assertEqual(repaired["status"], "PASS")
        self.assertEqual(
            set(details["pruned_intermediate_outputs"]),
            {"first_name", "last_name"},
        )
        self.assertNotIn("first_name", repaired_sql)
        self.assertNotIn("last_name", repaired_sql)

    def test_unknown_source_fails_closed(self):
        result = evaluate("SELECT mystery FROM other_table")
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(result["unresolved_outputs"])


if __name__ == "__main__":
    unittest.main()
