import sys
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from context_builder import extract_downstream_assets, select_dataset


def result(name: str):
    return {
        "entity": {
            "urn": f"urn:li:dataset:(urn:li:dataPlatform:dbt,{name},PROD)",
            "name": name,
        }
    }


class ContextSelectionTests(unittest.TestCase):
    def test_exact_short_name_wins_over_search_order(self):
        payload = {
            "searchResults": [
                result("shop.analytics.customer_value_dashboard"),
                result("shop.main.customers"),
            ]
        }
        self.assertEqual(
            select_dataset(payload, "customers")["name"], "shop.main.customers"
        )

    def test_falls_back_when_no_exact_name_exists(self):
        payload = {"searchResults": [result("shop.analytics.customer_report")]}
        self.assertEqual(
            select_dataset(payload, "customers")["name"],
            "shop.analytics.customer_report",
        )

    def test_fully_qualified_same_short_name_is_representation(self):
        lineage = {
            "downstreams": {
                "searchResults": [
                    {
                        "entity": {
                            "urn": "urn:li:dataset:(urn:li:dataPlatform:duckdb,shop.main.customers,PROD)",
                            "name": "shop.main.customers",
                            "type": "DATASET",
                        },
                        "degree": 1,
                    }
                ]
            }
        }
        assets = extract_downstream_assets(lineage, "customers")
        self.assertTrue(assets[0]["is_representation"])


if __name__ == "__main__":
    unittest.main()
