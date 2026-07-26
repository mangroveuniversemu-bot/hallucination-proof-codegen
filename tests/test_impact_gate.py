import sys
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from impact_gate import assess_impact


SOURCE = "urn:li:dataset:(urn:li:dataPlatform:dbt,shop.customers,PROD)"


def asset(name: str, criticality: str | None, representation: bool = False):
    urn = f"urn:li:dataset:(urn:li:dataPlatform:dbt,shop.{name},PROD)"
    return {
        "urn": urn,
        "name": f"shop.{name}",
        "type": "DATASET",
        "degree": 1,
        "criticality": criticality,
        "is_representation": representation,
        "lineage_path": [
            {"urn": SOURCE, "type": "DATASET"},
            {"urn": urn, "type": "DATASET"},
        ],
    }


POLICY = {
    "policy_id": "test",
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


class ImpactGateTests(unittest.TestCase):
    def assess(self, assets):
        return assess_impact(
            {"urn": SOURCE, "lineage_max_hops": 3, "downstream_assets": assets},
            POLICY,
        )

    def test_low_can_auto_pr(self):
        self.assertEqual(self.assess([asset("dashboard", "LOW")])["admission_action"], "AUTO_PR")

    def test_medium_requires_reviewer(self):
        self.assertEqual(
            self.assess([asset("report", "MEDIUM")])["admission_action"],
            "REVIEW_REQUIRED",
        )

    def test_high_blocks_auto_merge(self):
        result = self.assess(
            [asset("dashboard", "LOW"), asset("features", "HIGH")]
        )
        self.assertEqual(result["status"], "BLOCK")
        self.assertEqual(result["highest_criticality"], "HIGH")

    def test_representation_copy_is_ignored(self):
        result = self.assess([asset("customers", "HIGH", representation=True)])
        self.assertEqual(result["admission_action"], "AUTO_PR")
        self.assertEqual(result["ignored_assets"][0]["reason"], "representation_copy")

    def test_unknown_criticality_defaults_to_review(self):
        result = self.assess([asset("unknown", None)])
        self.assertEqual(result["admission_action"], "REVIEW_REQUIRED")
        self.assertEqual(
            result["affected_assets"][0]["criticality_source"], "policy_default"
        )

    def test_unverified_low_path_escalates_to_review(self):
        item = asset("dashboard", "LOW")
        item["lineage_path"] = []
        self.assertEqual(
            self.assess([item])["admission_action"], "REVIEW_REQUIRED"
        )


if __name__ == "__main__":
    unittest.main()
