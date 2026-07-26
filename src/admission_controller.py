import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

from codegen import build_grounded_system_prompt, generate_sql
from governance_gate import evaluate_governance, load_context, load_policy
from impact_gate import assess_impact, load_json as load_impact_json
from validator import ValidationError, validate_sql


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTEXT_PATH = PROJECT_ROOT / "examples" / "context_bundle.json"
DEFAULT_GOVERNANCE_POLICY = PROJECT_ROOT / "policies" / "pii_direct_projection.json"
DEFAULT_IMPACT_POLICY = PROJECT_ROOT / "policies" / "impact_policy.json"
DEFAULT_REPAIR_OUTPUT = PROJECT_ROOT / "examples" / "output_agent_repaired.sql"
DEFAULT_REPORT_OUTPUT = PROJECT_ROOT / "examples" / "admission_report.json"
RepairFunction = Callable[[str, dict[str, Any], dict[str, Any], str], str]


class AdmissionError(RuntimeError):
    pass


PATH_KEYS = {
    "candidate_file",
    "context_file",
    "output_file",
    "policy_file",
    "sql_file",
}


def _portable_path(value: str) -> str:
    path = Path(value)
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def portable_report(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            item_key: portable_report(item, item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [portable_report(item) for item in value]
    if key in PATH_KEYS and isinstance(value, str):
        return _portable_path(value)
    return value


def _classification_key(value: str) -> str:
    return value.rsplit(":", maxsplit=1)[-1].upper()


def schema_failure_report(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "gate": "schema",
        "status": "BLOCK",
        "violations": [
            {
                "field": item["column"],
                "reasons": item["reasons"],
                "allowed_actions": ["replace_with_allowed_field", "exclude"],
            }
            for item in result.get("invalid_details", [])
        ],
        "allowed_source_fields": result.get("allowed_columns", []),
        "repair_attempts_allowed": 1,
    }


def governance_failure_report(
    result: dict[str, Any],
    policy: dict[str, Any],
    table_name: str,
) -> dict[str, Any]:
    actions_by_classification = policy.get(
        "allowed_actions_by_classification", {}
    )
    violations: list[dict[str, Any]] = []
    for item in result.get("violations", []):
        classifications = item.get("matched_classifications", [])
        allowed: list[str] = []
        for classification in classifications:
            if not isinstance(classification, str):
                continue
            allowed.extend(
                actions_by_classification.get(
                    _classification_key(classification), []
                )
            )
        violations.append(
            {
                "field": f"{table_name}.{item.get('source_column')}",
                "output_column": item.get("output_column"),
                "classification": classifications,
                "allowed_actions": list(dict.fromkeys(allowed)),
            }
        )
    return {
        "gate": "governance",
        "status": "BLOCK",
        "policy_id": policy.get("policy_id"),
        "violations": violations,
        "repair_attempts_allowed": 1,
    }


def repair_with_glm(
    original_sql: str,
    failure_report: dict[str, Any],
    context: dict[str, Any],
    task: str,
) -> str:
    system_prompt = (
        build_grounded_system_prompt(context)
        + "\nYou are the bounded repair stage of an automated admission controller.\n"
        + "You have exactly one repair attempt. Apply only actions explicitly listed "
        + "in the structured gate report. Preserve all unaffected business logic. "
        + "If an allowed repair cannot be made safely, return a SQL comment beginning "
        + "with -- BLOCKED:. Return SQL only.\n"
    )
    repair_task = (
        f"Original business task:\n{task}\n\n"
        "Structured gate failure report:\n"
        f"{json.dumps(failure_report, indent=2, ensure_ascii=False)}\n\n"
        "Original candidate SQL:\n"
        f"{original_sql}\n\n"
        "Produce the one repaired SQL candidate now."
    )
    return generate_sql(repair_task, system_prompt)


def _gate_code(
    sql_path: Path,
    context_path: Path,
    context: dict[str, Any],
    governance_policy: dict[str, Any],
    dialect: str,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    schema = validate_sql(sql_path, context_path, dialect)
    if schema["status"] != "PASS":
        return schema, None
    sql = sql_path.read_text(encoding="utf-8")
    governance = evaluate_governance(
        sql, context, governance_policy, dialect
    )
    return schema, governance


def run_admission(
    *,
    sql_path: Path,
    context_path: Path,
    governance_policy_path: Path,
    impact_policy_path: Path,
    repair_output: Path,
    task: str,
    dialect: str = "duckdb",
    repair_function: RepairFunction = repair_with_glm,
    enable_repair: bool = True,
) -> dict[str, Any]:
    context = load_context(context_path)
    governance_policy = load_policy(governance_policy_path)
    impact_policy = load_impact_json(impact_policy_path, "Impact policy")
    impact = assess_impact(context, impact_policy)
    original_schema, original_governance = _gate_code(
        sql_path,
        context_path,
        context,
        governance_policy,
        dialect,
    )

    failure_report: dict[str, Any] | None = None
    if original_schema["status"] != "PASS":
        failure_report = schema_failure_report(original_schema)
    elif original_governance and original_governance["status"] != "PASS":
        failure_report = governance_failure_report(
            original_governance,
            governance_policy,
            context["table_name"],
        )

    repair: dict[str, Any] = {
        "bounded": True,
        "max_attempts": 1,
        "attempts": 0,
        "provider": "glm",
        "status": "NOT_NEEDED" if failure_report is None else "NOT_ATTEMPTED",
    }
    final_schema = original_schema
    final_governance = original_governance

    if failure_report is not None and enable_repair:
        repairable = bool(failure_report.get("violations")) and all(
            violation.get("allowed_actions")
            for violation in failure_report["violations"]
        )
        if repairable:
            repair["attempts"] = 1
            original_sql = sql_path.read_text(encoding="utf-8")
            try:
                repaired_sql = repair_function(
                    original_sql,
                    failure_report,
                    context,
                    task,
                )
                repair_output.parent.mkdir(parents=True, exist_ok=True)
                repair_output.write_text(
                    repaired_sql.rstrip() + "\n", encoding="utf-8"
                )
                final_schema, final_governance = _gate_code(
                    repair_output,
                    context_path,
                    context,
                    governance_policy,
                    dialect,
                )
                passed = final_schema["status"] == "PASS" and bool(
                    final_governance and final_governance["status"] == "PASS"
                )
                repair.update(
                    {
                        "status": "PASS" if passed else "FAIL",
                        "output_file": repair_output.as_posix(),
                    }
                )
            except Exception as exc:  # Boundary around one external repair call.
                repair.update(
                    {
                        "status": "ERROR",
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
        else:
            repair["status"] = "BLOCKED_NO_ALLOWED_REPAIR"

    code_passed = final_schema["status"] == "PASS" and bool(
        final_governance and final_governance["status"] == "PASS"
    )
    if not code_passed:
        admission_action = "BLOCK_AUTO_MERGE"
        status = "BLOCK"
        reason = "Schema or governance gate did not pass after the bounded repair."
    else:
        admission_action = impact["admission_action"]
        status = impact["status"]
        reason = impact["message"]

    return portable_report({
        "controller": "metadata_aware_change_admission",
        "status": status,
        "admission_action": admission_action,
        "reason": reason,
        "task": task,
        "candidate_file": sql_path.as_posix(),
        "gates": {
            "initial_schema": original_schema,
            "initial_governance": original_governance,
            "final_schema": final_schema,
            "final_governance": final_governance,
            "downstream_impact": impact,
        },
        "structured_failure_report": failure_report,
        "repair": repair,
    })


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run schema, governance, one bounded GLM repair, and downstream "
            "impact admission."
        )
    )
    parser.add_argument("sql_file", type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT_PATH)
    parser.add_argument(
        "--governance-policy", type=Path, default=DEFAULT_GOVERNANCE_POLICY
    )
    parser.add_argument("--impact-policy", type=Path, default=DEFAULT_IMPACT_POLICY)
    parser.add_argument("--repair-output", type=Path, default=DEFAULT_REPAIR_OUTPUT)
    parser.add_argument("--report-output", type=Path, default=DEFAULT_REPORT_OUTPUT)
    parser.add_argument("--dialect", default="duckdb")
    parser.add_argument("--no-repair", action="store_true")
    args = parser.parse_args()
    try:
        result = run_admission(
            sql_path=args.sql_file,
            context_path=args.context,
            governance_policy_path=args.governance_policy,
            impact_policy_path=args.impact_policy,
            repair_output=args.repair_output,
            task=args.task,
            dialect=args.dialect,
            enable_repair=not args.no_repair,
        )
    except (OSError, AdmissionError, RuntimeError, ValidationError) as exc:
        result = {
            "controller": "metadata_aware_change_admission",
            "status": "ERROR",
            "admission_action": "BLOCK_AUTO_MERGE",
            "message": str(exc),
        }
        exit_code = 2
    else:
        exit_code = {
            "AUTO_PR": 0,
            "BLOCK_AUTO_MERGE": 1,
            "REVIEW_REQUIRED": 3,
        }[result["admission_action"]]
    rendered = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    print(rendered, end="")
    args.report_output.parent.mkdir(parents=True, exist_ok=True)
    args.report_output.write_text(rendered, encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
