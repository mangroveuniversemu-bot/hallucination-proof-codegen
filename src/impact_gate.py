import argparse
import json
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTEXT_PATH = PROJECT_ROOT / "examples" / "context_bundle.json"
DEFAULT_POLICY_PATH = PROJECT_ROOT / "policies" / "impact_policy.json"
CRITICALITY_ORDER = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
ACTION_ORDER = {"AUTO_PR": 1, "REVIEW_REQUIRED": 2, "BLOCK_AUTO_MERGE": 3}
STATUS_BY_ACTION = {
    "AUTO_PR": "PASS",
    "REVIEW_REQUIRED": "REVIEW",
    "BLOCK_AUTO_MERGE": "BLOCK",
}
EXIT_BY_ACTION = {"AUTO_PR": 0, "REVIEW_REQUIRED": 3, "BLOCK_AUTO_MERGE": 1}


class ImpactError(RuntimeError):
    pass


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ImpactError(f"{label} file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ImpactError(f"{label} contains invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ImpactError(f"{label} must contain a JSON object")
    return value


def validate_policy(policy: dict[str, Any]) -> None:
    actions = policy.get("actions")
    if not isinstance(actions, dict):
        raise ImpactError("Impact policy is missing actions")
    for criticality in CRITICALITY_ORDER:
        action = actions.get(criticality)
        if action not in ACTION_ORDER:
            raise ImpactError(f"Invalid action for {criticality}: {action}")
    if policy.get("default_criticality") not in CRITICALITY_ORDER:
        raise ImpactError("Impact policy has an invalid default_criticality")
    if policy.get("no_downstream_action") not in ACTION_ORDER:
        raise ImpactError("Impact policy has an invalid no_downstream_action")


def path_is_verified(source_urn: str, target_urn: str, path: Any) -> bool:
    if not isinstance(path, list) or len(path) < 2:
        return False
    urns = [item.get("urn") for item in path if isinstance(item, dict)]
    return bool(urns and urns[0] == source_urn and urns[-1] == target_urn)


def assess_impact(
    context: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    validate_policy(policy)
    source_urn = context.get("urn")
    assets = context.get("downstream_assets", [])
    if not isinstance(source_urn, str) or not source_urn:
        raise ImpactError("Context is missing the source dataset urn")
    if not isinstance(assets, list):
        raise ImpactError("Context downstream_assets must be a list")

    affected: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    default_criticality = policy["default_criticality"]
    require_paths = bool(policy.get("require_verified_paths", True))

    for raw_asset in assets:
        if not isinstance(raw_asset, dict):
            continue
        if policy.get("ignore_representation_copies") and raw_asset.get(
            "is_representation"
        ):
            ignored.append(
                {
                    "urn": raw_asset.get("urn"),
                    "name": raw_asset.get("name"),
                    "reason": "representation_copy",
                }
            )
            continue

        urn = raw_asset.get("urn")
        name = raw_asset.get("name")
        if not isinstance(urn, str) or not isinstance(name, str):
            raise ImpactError("Every downstream asset must have urn and name")
        declared = raw_asset.get("criticality")
        criticality = declared if declared in CRITICALITY_ORDER else default_criticality
        criticality_source = "datahub" if declared in CRITICALITY_ORDER else "policy_default"
        verified = path_is_verified(source_urn, urn, raw_asset.get("lineage_path"))
        action = policy["actions"][criticality]
        if require_paths and not verified and ACTION_ORDER[action] < ACTION_ORDER["REVIEW_REQUIRED"]:
            action = "REVIEW_REQUIRED"

        affected.append(
            {
                "urn": urn,
                "name": name,
                "type": raw_asset.get("type", "UNKNOWN"),
                "degree": raw_asset.get("degree"),
                "criticality": criticality,
                "criticality_source": criticality_source,
                "lineage_path_verified": verified,
                "lineage_path": raw_asset.get("lineage_path", []),
                "required_action": action,
            }
        )

    if affected:
        admission_action = max(
            (asset["required_action"] for asset in affected),
            key=ACTION_ORDER.__getitem__,
        )
        highest = max(
            (asset["criticality"] for asset in affected),
            key=CRITICALITY_ORDER.__getitem__,
        )
    else:
        admission_action = policy["no_downstream_action"]
        highest = None

    return {
        "gate": "downstream_impact",
        "status": STATUS_BY_ACTION[admission_action],
        "policy_id": policy.get("policy_id", "unknown"),
        "source_urn": source_urn,
        "lineage_max_hops": context.get("lineage_max_hops"),
        "may_affect": [asset["name"] for asset in affected],
        "affected_assets": affected,
        "ignored_assets": ignored,
        "highest_criticality": highest,
        "admission_action": admission_action,
        "message": {
            "AUTO_PR": "No downstream risk requires human admission review.",
            "REVIEW_REQUIRED": "At least one downstream consumer requires a reviewer.",
            "BLOCK_AUTO_MERGE": "A HIGH-criticality downstream consumer blocks automatic merge.",
        }[admission_action],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Route a model change using DataHub downstream lineage and criticality."
    )
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT_PATH)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--report-output", type=Path)
    args = parser.parse_args()
    try:
        result = assess_impact(
            load_json(args.context, "Context"),
            load_json(args.policy, "Policy"),
        )
    except (OSError, ImpactError) as exc:
        result = {"gate": "downstream_impact", "status": "ERROR", "message": str(exc)}
        exit_code = 2
    else:
        exit_code = EXIT_BY_ACTION[result["admission_action"]]
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    print(rendered, end="")
    if args.report_output:
        args.report_output.parent.mkdir(parents=True, exist_ok=True)
        args.report_output.write_text(rendered, encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
