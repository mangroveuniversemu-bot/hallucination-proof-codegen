import json
import sys
import tempfile
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from validator import validate_sql


class ValidatorDerivedStarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.context = self.root / "context.json"
        self.sql = self.root / "candidate.sql"
        self.context.write_text(
            json.dumps(
                {
                    "table_name": "checkouts",
                    "urn": "urn:benchmark:checkouts",
                    "fields": [
                        {"fieldPath": "shopper_fk"},
                        {"fieldPath": "gross_revenue_usd"},
                    ],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_cte_select_star_forwards_derived_outputs(self):
        self.sql.write_text(
            "WITH totals AS ("
            "SELECT shopper_fk AS shopper_id, "
            "COUNT(*) AS checkout_count, "
            "SUM(gross_revenue_usd) AS total_revenue "
            "FROM checkouts GROUP BY shopper_fk"
            "), ranked AS ("
            "SELECT *, ROW_NUMBER() OVER (ORDER BY total_revenue DESC) AS rn "
            "FROM totals"
            ") SELECT shopper_id, checkout_count, total_revenue "
            "FROM ranked WHERE rn <= 2",
            encoding="utf-8",
        )
        result = validate_sql(self.sql, self.context, "duckdb")
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["hallucinated_columns"], [])

    def test_missing_derived_output_still_fails(self):
        self.sql.write_text(
            "WITH totals AS (SELECT shopper_fk FROM checkouts) "
            "SELECT invented_metric FROM totals",
            encoding="utf-8",
        )
        result = validate_sql(self.sql, self.context, "duckdb")
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("invented_metric", result["hallucinated_columns"])


if __name__ == "__main__":
    unittest.main()
