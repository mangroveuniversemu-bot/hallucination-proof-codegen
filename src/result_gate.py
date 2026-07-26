import argparse
import datetime as dt
import decimal
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import duckdb

from validator import render_dbt_relations


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTRACT_PATH = PROJECT_ROOT / "demo" / "result_contract.json"


class ResultGateError(RuntimeError):
    pass


def load_contract(path: Path) -> dict[str, Any]:
    try:
        contract = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ResultGateError(f"Result contract not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ResultGateError(f"Result contract contains invalid JSON: {path}") from exc
    if not isinstance(contract, dict):
        raise ResultGateError("Result contract must be a JSON object")
    if not isinstance(contract.get("contract_id"), str):
        raise ResultGateError("Result contract is missing contract_id")
    return contract


def json_value(value: Any) -> Any:
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    return value


def execute_duckdb(sql: str, database_path: Path) -> dict[str, Any]:
    if not database_path.is_file():
        raise ResultGateError(f"DuckDB database not found: {database_path}")
    connection = duckdb.connect(str(database_path), read_only=True)
    try:
        cursor = connection.execute(render_dbt_relations(sql))
        columns = [item[0] for item in (cursor.description or [])]
        rows = cursor.fetchall()
    except Exception as exc:
        return {
            "gate": "runtime",
            "status": "FAIL",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    finally:
        connection.close()
    return {
        "gate": "runtime",
        "status": "PASS",
        "columns": columns,
        "row_count": len(rows),
        "rows": rows,
        "preview": [
            [json_value(value) for value in row]
            for row in rows[:5]
        ],
    }


def _column_lookup(columns: list[str]) -> tuple[dict[str, int], list[str]]:
    lookup: dict[str, int] = {}
    duplicates: list[str] = []
    for index, name in enumerate(columns):
        key = name.casefold()
        if key in lookup:
            duplicates.append(name)
        else:
            lookup[key] = index
    return lookup, duplicates


def _expected_count_map(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ResultGateError("value_counts entries must be objects")
    result: dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(count, int) or count < 0:
            raise ResultGateError("value_counts values must be non-negative integers")
        result[str(key)] = count
    return result


def evaluate_result_contract(
    execution: dict[str, Any],
    contract: dict[str, Any],
) -> dict[str, Any]:
    if execution.get("status") != "PASS":
        return {
            "gate": "result_quality",
            "status": "SKIPPED",
            "contract_id": contract.get("contract_id"),
            "message": "Runtime gate did not pass.",
            "violations": [],
        }

    columns = execution.get("columns", [])
    rows = execution.get("rows", [])
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ResultGateError("Runtime result is missing columns or rows")
    lookup, duplicates = _column_lookup(columns)
    violations: list[dict[str, Any]] = []

    if duplicates:
        violations.append(
            {
                "type": "duplicate_output_columns",
                "columns": sorted(duplicates),
                "allowed_actions": ["rename_output_columns"],
            }
        )

    required = contract.get("required_columns", [])
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ResultGateError("required_columns must be a list of strings")
    missing = [name for name in required if name.casefold() not in lookup]
    if missing:
        violations.append(
            {
                "type": "missing_required_columns",
                "columns": missing,
                "allowed_actions": ["add_required_output_aliases"],
            }
        )

    if not contract.get("allow_extra_columns", True):
        required_keys = {name.casefold() for name in required}
        extras = [name for name in columns if name.casefold() not in required_keys]
        if extras:
            violations.append(
                {
                    "type": "unexpected_output_columns",
                    "columns": extras,
                    "allowed_actions": ["exclude"],
                }
            )

    exact_row_count = contract.get("exact_row_count")
    if exact_row_count is not None:
        if not isinstance(exact_row_count, int) or exact_row_count < 0:
            raise ResultGateError("exact_row_count must be a non-negative integer")
        if len(rows) != exact_row_count:
            violations.append(
                {
                    "type": "row_count_mismatch",
                    "expected": exact_row_count,
                    "actual": len(rows),
                    "allowed_actions": ["preserve_one_row_per_entity"],
                }
            )

    null_quality: dict[str, Any] = {}
    non_null_columns = contract.get("non_null_columns", [])
    if not isinstance(non_null_columns, list):
        raise ResultGateError("non_null_columns must be a list")
    null_defaults = contract.get("null_defaults", {})
    if not isinstance(null_defaults, dict):
        raise ResultGateError("null_defaults must be an object")
    for name in non_null_columns:
        if not isinstance(name, str) or name.casefold() not in lookup:
            continue
        index = lookup[name.casefold()]
        null_rows = [row_number for row_number, row in enumerate(rows, start=1) if row[index] is None]
        null_record: dict[str, Any] = {
            "status": "PASS" if not null_rows else "FAIL",
            "null_count": len(null_rows),
            "sample_row_numbers": null_rows[:5],
        }
        default_spec = null_defaults.get(name)
        if isinstance(default_spec, dict):
            if "value" not in default_spec:
                raise ResultGateError(
                    f"null_defaults.{name} must contain a value"
                )
            declared_default = default_spec["value"]
            expected_replacements = default_spec.get("expected_replacements")
        else:
            declared_default = default_spec
            expected_replacements = None

        if expected_replacements is not None:
            if not isinstance(expected_replacements, int) or expected_replacements < 0:
                raise ResultGateError(
                    f"null_defaults.{name}.expected_replacements must be a "
                    "non-negative integer"
                )
            actual_replacements = sum(
                1 for row in rows if row[index] == declared_default
            )
            replacement_status = (
                "PASS" if actual_replacements == expected_replacements else "FAIL"
            )
            null_record["declared_default"] = declared_default
            null_record["expected_replacements"] = expected_replacements
            null_record["actual_replacements"] = actual_replacements
            null_record["default_replacement_status"] = replacement_status
            if replacement_status == "FAIL":
                null_record["status"] = "FAIL"
                violations.append(
                    {
                        "type": "null_default_count_mismatch",
                        "column": name,
                        "declared_default": declared_default,
                        "expected_replacements": expected_replacements,
                        "actual_replacements": actual_replacements,
                        "allowed_actions": ["apply_declared_null_default"],
                    }
                )
        null_quality[name] = null_record
        if null_rows:
            violation: dict[str, Any] = {
                "type": "unexpected_nulls",
                "column": name,
                "null_count": len(null_rows),
                "sample_row_numbers": null_rows[:5],
                "allowed_actions": ["apply_declared_null_default"],
            }
            if name in null_defaults:
                violation["declared_default"] = declared_default
            violations.append(violation)

    uniqueness: dict[str, Any] = {}
    unique_columns = contract.get("unique_columns", [])
    if not isinstance(unique_columns, list):
        raise ResultGateError("unique_columns must be a list")
    for name in unique_columns:
        if not isinstance(name, str) or name.casefold() not in lookup:
            continue
        index = lookup[name.casefold()]
        values = [row[index] for row in rows]
        duplicate_count = len(values) - len(set(values))
        uniqueness[name] = {
            "status": "PASS" if duplicate_count == 0 else "FAIL",
            "duplicate_count": duplicate_count,
        }
        if duplicate_count:
            violations.append(
                {
                    "type": "uniqueness_violation",
                    "column": name,
                    "duplicate_count": duplicate_count,
                    "allowed_actions": ["preserve_one_row_per_entity"],
                }
            )

    range_results: dict[str, Any] = {}
    numeric_ranges = contract.get("numeric_ranges", {})
    if not isinstance(numeric_ranges, dict):
        raise ResultGateError("numeric_ranges must be an object")
    for name, bounds in numeric_ranges.items():
        if name.casefold() not in lookup:
            continue
        if not isinstance(bounds, dict):
            raise ResultGateError("numeric range bounds must be objects")
        minimum = bounds.get("min")
        maximum = bounds.get("max")
        index = lookup[name.casefold()]
        invalid_rows: list[int] = []
        for row_number, row in enumerate(rows, start=1):
            value = row[index]
            if value is None:
                continue
            if not isinstance(value, (int, float, decimal.Decimal)):
                invalid_rows.append(row_number)
                continue
            if minimum is not None and value < minimum:
                invalid_rows.append(row_number)
            elif maximum is not None and value > maximum:
                invalid_rows.append(row_number)
        range_results[name] = {
            "status": "PASS" if not invalid_rows else "FAIL",
            "minimum": minimum,
            "maximum": maximum,
            "invalid_count": len(invalid_rows),
        }
        if invalid_rows:
            violations.append(
                {
                    "type": "numeric_range_violation",
                    "column": name,
                    "invalid_count": len(invalid_rows),
                    "sample_row_numbers": invalid_rows[:5],
                    "allowed_actions": ["correct_calculation"],
                }
            )

    count_results: dict[str, Any] = {}
    value_counts = contract.get("value_counts", {})
    if not isinstance(value_counts, dict):
        raise ResultGateError("value_counts must be an object")
    for name, expected_value_counts in value_counts.items():
        if name.casefold() not in lookup:
            continue
        expected = _expected_count_map(expected_value_counts)
        index = lookup[name.casefold()]
        actual_counter = Counter(str(row[index]) for row in rows)
        actual = dict(sorted(actual_counter.items()))
        count_results[name] = {
            "status": "PASS" if actual == expected else "FAIL",
            "expected": expected,
            "actual": actual,
        }
        if actual != expected:
            violations.append(
                {
                    "type": "value_count_mismatch",
                    "column": name,
                    "expected": expected,
                    "actual": actual,
                    "allowed_actions": ["correct_calculation"],
                }
            )

    status = "PASS" if not violations else "FAIL"
    return {
        "gate": "result_quality",
        "status": status,
        "contract_id": contract["contract_id"],
        "row_count": len(rows),
        "columns": columns,
        "required_columns": required,
        "null_quality": null_quality,
        "uniqueness": uniqueness,
        "numeric_ranges": range_results,
        "value_counts": count_results,
        "violations": violations,
        "message": (
            "Runtime output satisfies the result and NULL-quality contract."
            if status == "PASS"
            else "Runtime output violates the result or NULL-quality contract."
        ),
    }


def public_runtime_report(execution: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in execution.items() if key != "rows"}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Execute DuckDB SQL and enforce an explicit result/NULL contract."
    )
    parser.add_argument("sql_file", type=Path)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT_PATH)
    args = parser.parse_args()
    try:
        sql = args.sql_file.read_text(encoding="utf-8")
        contract = load_contract(args.contract)
        execution = execute_duckdb(sql, args.database)
        result = {
            "runtime": public_runtime_report(execution),
            "result_quality": evaluate_result_contract(execution, contract),
        }
    except (OSError, ResultGateError, ValueError) as exc:
        result = {"status": "ERROR", "message": str(exc)}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if all(item.get("status") == "PASS" for item in result.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
