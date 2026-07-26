import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from sqlglot import exp, parse_one
from sqlglot.errors import ParseError, SqlglotError
from sqlglot.lineage import Node, lineage

from validator import render_dbt_relations


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTEXT_PATH = PROJECT_ROOT / "examples" / "context_bundle.json"
DEFAULT_POLICY_PATH = PROJECT_ROOT / "policies" / "pii_direct_projection.json"
DEFAULT_REPAIR_PATH = PROJECT_ROOT / "examples" / "output_pii_repaired.sql"


class GovernanceError(RuntimeError):
    pass


def load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GovernanceError(f"{label} file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise GovernanceError(f"{label} file contains invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise GovernanceError(f"{label} must contain a JSON object")
    return value


def load_context(path: Path) -> dict[str, Any]:
    context = load_json_object(path, "Context")
    table_name = context.get("table_name")
    fields = context.get("fields")
    if not isinstance(table_name, str) or not table_name:
        raise GovernanceError("Context is missing a non-empty table_name")
    if not isinstance(fields, list) or not fields:
        raise GovernanceError("Context is missing a non-empty fields list")
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("fieldPath"), str):
            raise GovernanceError("Every context field must have a fieldPath")
    return context


def load_policy(path: Path) -> dict[str, Any]:
    policy = load_json_object(path, "Policy")
    if not isinstance(policy.get("policy_id"), str):
        raise GovernanceError("Policy is missing policy_id")
    denied = policy.get("denied_classifications")
    if not isinstance(denied, list) or not all(isinstance(item, str) for item in denied):
        raise GovernanceError("Policy denied_classifications must be a list of strings")
    repair = policy.get("repair")
    if not isinstance(repair, dict):
        raise GovernanceError("Policy is missing a repair object")
    if repair.get("strategy") != "drop_projection":
        raise GovernanceError("Only the drop_projection repair strategy is supported")
    return policy


def _entity_tokens(value: Any) -> set[str]:
    tokens: set[str] = set()
    if isinstance(value, str):
        if value:
            tokens.add(value)
        return tokens
    if not isinstance(value, dict):
        return tokens

    for key in ("urn", "name"):
        item = value.get(key)
        if isinstance(item, str) and item:
            tokens.add(item)

    properties = value.get("properties")
    if isinstance(properties, dict):
        name = properties.get("name")
        if isinstance(name, str) and name:
            tokens.add(name)
    return tokens


def _association_tokens(
    value: Any,
    *,
    collection_key: str,
    entity_key: str,
) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, list):
        entries = value
    elif isinstance(value, dict):
        entries = value.get(collection_key, [])
        if not entries:
            return _entity_tokens(value)
    else:
        return _entity_tokens(value)

    tokens: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            tokens.update(_entity_tokens(entry.get(entity_key, entry)))
        else:
            tokens.update(_entity_tokens(entry))
    return tokens


def field_classifications(field: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for key in ("tags", "globalTags", "editedTags"):
        tokens.update(
            _association_tokens(
                field.get(key),
                collection_key="tags",
                entity_key="tag",
            )
        )
    tokens.update(
        _association_tokens(
            field.get("glossaryTerms"),
            collection_key="terms",
            entity_key="term",
        )
    )
    tokens.update(
        _association_tokens(
            field.get("editedGlossaryTerms"),
            collection_key="terms",
            entity_key="term",
        )
    )
    normalized = field.get("classifications")
    if isinstance(normalized, list):
        tokens.update(item for item in normalized if isinstance(item, str) and item)
    return tokens


def _schema_from_context(context: dict[str, Any]) -> dict[str, dict[str, str]]:
    fields: dict[str, str] = {}
    for field in context["fields"]:
        field_path = field["fieldPath"]
        native_type = field.get("nativeDataType")
        fields[field_path] = native_type if isinstance(native_type, str) else "UNKNOWN"
    return {context["table_name"]: fields}


def _leaf_sources(node: Node) -> tuple[set[str], set[str]]:
    source_columns: set[str] = set()
    unresolved: set[str] = set()

    for candidate in node.walk():
        if candidate.downstream:
            continue
        if isinstance(candidate.expression, exp.Table):
            column_name = candidate.name.rsplit(".", maxsplit=1)[-1].strip('"`[]')
            if column_name and column_name != "*":
                source_columns.add(column_name)
            else:
                unresolved.add(candidate.name or "*")
            continue
        if isinstance(candidate.expression, exp.Placeholder):
            unresolved.add(candidate.name or "<unknown>")
            continue
        # A leaf expression with no column dependency (for example COUNT(*),
        # ROW_NUMBER(), or a literal) cannot directly expose a classified field.
        # Unknown placeholders and stars are handled fail-closed above.
        if not list(candidate.expression.find_all(exp.Column)):
            continue
        if isinstance(candidate.expression, exp.Star):
            unresolved.add(candidate.name or "*")
            continue
        if candidate is not node:
            unresolved.add(candidate.name or candidate.expression.sql())

    return source_columns, unresolved


def evaluate_governance(
    sql: str,
    context: dict[str, Any],
    policy: dict[str, Any],
    dialect: str = "duckdb",
) -> dict[str, Any]:
    table_name = context["table_name"]
    fields_by_key = {
        field["fieldPath"].casefold(): field for field in context["fields"]
    }
    denied = {
        classification.casefold()
        for classification in policy["denied_classifications"]
    }

    field_tokens: dict[str, set[str]] = {}
    sensitive_fields: list[dict[str, Any]] = []
    for key, field in fields_by_key.items():
        tokens = field_classifications(field)
        field_tokens[key] = tokens
        matched = sorted(
            token for token in tokens if token.casefold() in denied
        )
        if matched:
            sensitive_fields.append(
                {
                    "field": field["fieldPath"],
                    "matched_classifications": matched,
                }
            )

    try:
        rendered_sql = render_dbt_relations(sql)
        output_graphs = lineage(
            None,
            rendered_sql,
            schema=_schema_from_context(context),
            dialect=dialect,
            trim_selects=False,
        )
    except (ParseError, SqlglotError, ValueError) as exc:
        raise GovernanceError(f"SQL lineage could not be resolved: {exc}") from exc

    output_lineage: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    unresolved_outputs: list[dict[str, Any]] = []

    for output_name, graph in output_graphs.items():
        roots, unresolved = _leaf_sources(graph)
        output_record = {
            "output_column": output_name,
            "source_columns": sorted(roots),
            "unresolved_sources": sorted(unresolved),
        }
        output_lineage.append(output_record)

        if unresolved:
            unresolved_outputs.append(output_record)

        for root in sorted(roots):
            field = fields_by_key.get(root.casefold())
            if field is None:
                unresolved_outputs.append(
                    {
                        "output_column": output_name,
                        "source_columns": [root],
                        "unresolved_sources": ["not_in_context_schema"],
                    }
                )
                continue

            matched = sorted(
                token
                for token in field_tokens[root.casefold()]
                if token.casefold() in denied
            )
            if matched:
                violations.append(
                    {
                        "output_column": output_name,
                        "source_column": field["fieldPath"],
                        "matched_classifications": matched,
                        "reason": "sensitive_source_reaches_final_output",
                    }
                )

    fail_closed = bool(policy.get("fail_closed_on_unresolved_lineage", True))
    status = (
        "FAIL"
        if violations or (fail_closed and unresolved_outputs)
        else "PASS"
    )
    return {
        "gate": "governance",
        "status": status,
        "policy_id": policy["policy_id"],
        "policy_scope": policy.get("scope", "final_output"),
        "table_name": table_name,
        "sensitive_fields": sorted(
            sensitive_fields,
            key=lambda item: item["field"],
        ),
        "output_lineage": output_lineage,
        "violations": violations,
        "unresolved_outputs": unresolved_outputs,
        "repairable": bool(violations) and not unresolved_outputs,
        "message": (
            "All final outputs comply with the governance policy."
            if status == "PASS"
            else "One or more final outputs violate the governance policy."
        ),
    }


def _restore_dbt_refs(sql: str, table_name: str) -> str:
    table_pattern = re.escape(table_name)
    pattern = re.compile(
        rf"\b(?P<keyword>FROM|JOIN)\s+[\"`]?{table_pattern}[\"`]?\b",
        flags=re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        keyword = match.group("keyword").upper()
        return f"{keyword} {{{{ ref('{table_name}') }}}}"

    return pattern.sub(replace, sql)


def repair_sql(
    sql: str,
    evaluation: dict[str, Any],
    context: dict[str, Any],
    policy: dict[str, Any],
    dialect: str = "duckdb",
) -> tuple[str, dict[str, Any]]:
    if evaluation["status"] != "FAIL":
        raise GovernanceError("Repair was requested for SQL that already passes")
    if not evaluation["repairable"]:
        raise GovernanceError("The governance failure is not safely repairable")

    max_attempts = policy["repair"].get("max_attempts", 1)
    if max_attempts != 1:
        raise GovernanceError("Bounded repair requires max_attempts to equal 1")

    try:
        expression = parse_one(render_dbt_relations(sql), read=dialect)
    except ParseError as exc:
        raise GovernanceError(f"SQLGlot could not parse SQL for repair: {exc}") from exc
    if not isinstance(expression, exp.Select):
        raise GovernanceError("Bounded repair only supports a top-level SELECT")

    violating_outputs = {
        item["output_column"].casefold() for item in evaluation["violations"]
    }
    removed_outputs: list[str] = []
    remaining: list[exp.Expression] = []
    removed_explicit_aliases: set[str] = set()

    for select_expression in expression.selects:
        if select_expression.is_star:
            raise GovernanceError(
                "SELECT * is blocked but cannot be repaired without changing query shape"
            )
        output_name = select_expression.alias_or_name
        if output_name and output_name.casefold() in violating_outputs:
            removed_outputs.append(output_name)
            if isinstance(select_expression, exp.Alias):
                removed_explicit_aliases.add(output_name.casefold())
            continue
        remaining.append(select_expression)

    if not removed_outputs:
        raise GovernanceError("No violating top-level projections could be removed")
    if not remaining:
        raise GovernanceError("Repair would remove every output column")

    for clause_name in ("group", "order", "having", "qualify"):
        clause = expression.args.get(clause_name)
        if clause is None:
            continue
        referenced = {
            column.name.casefold()
            for column in clause.find_all(exp.Column)
            if column.name
        }
        if referenced & removed_explicit_aliases:
            raise GovernanceError(
                f"Repair would leave a broken {clause_name.upper()} reference"
            )

    expression.set("expressions", remaining)

    outer_dependencies: set[str] = set()
    for select_expression in expression.selects:
        outer_dependencies.update(
            column.name.casefold()
            for column in select_expression.find_all(exp.Column)
            if column.name
        )
    for clause_name, clause in expression.args.items():
        if clause_name in {"with", "with_", "expressions"} or not isinstance(
            clause, exp.Expression
        ):
            continue
        outer_dependencies.update(
            column.name.casefold()
            for column in clause.find_all(exp.Column)
            if column.name
        )

    pruned_intermediate_outputs: list[str] = []
    for cte in expression.ctes:
        cte_query = cte.this
        if not isinstance(cte_query, exp.Select):
            continue
        cte_remaining: list[exp.Expression] = []
        for cte_projection in cte_query.selects:
            cte_name = cte_projection.alias_or_name
            if (
                cte_name
                and cte_name.casefold() in violating_outputs
                and cte_name.casefold() not in outer_dependencies
            ):
                pruned_intermediate_outputs.append(cte_name)
                continue
            cte_remaining.append(cte_projection)
        if not cte_remaining:
            raise GovernanceError("Repair would empty a required CTE")
        cte_query.set("expressions", cte_remaining)

    replacement = policy["repair"].get("replacement_identity")
    added_identity: str | None = None
    projected = {
        item.alias_or_name.casefold()
        for item in expression.selects
        if item.alias_or_name
    }
    available = {
        field["fieldPath"].casefold(): field["fieldPath"]
        for field in context["fields"]
    }
    if (
        isinstance(replacement, str)
        and replacement.casefold() not in projected
        and replacement.casefold() in available
    ):
        if expression.args.get("group") or expression.args.get("distinct"):
            raise GovernanceError(
                "Repair cannot add an identity column to grouped or distinct SQL"
            )
        if any(item.find(exp.AggFunc) for item in expression.selects):
            raise GovernanceError(
                "Repair cannot add an identity column to aggregate SQL"
            )
        added_identity = available[replacement.casefold()]
        expression.select(exp.column(added_identity), append=True, copy=False)

    repaired = expression.sql(dialect=dialect, pretty=True)
    repaired = _restore_dbt_refs(repaired, context["table_name"]).rstrip() + "\n"
    return repaired, {
        "strategy": "drop_projection",
        "bounded": True,
        "attempts": 1,
        "removed_outputs": sorted(removed_outputs),
        "pruned_intermediate_outputs": sorted(pruned_intermediate_outputs),
        "added_identity": added_identity,
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def display_path(path: Path) -> str:
    """Prefer portable project-relative paths in reports and console output."""
    resolved = path.resolve()
    for base in (PROJECT_ROOT.resolve(), Path.cwd().resolve()):
        try:
            return resolved.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.as_posix()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Enforce field-level DataHub governance metadata on SQL outputs."
    )
    parser.add_argument("sql_file", type=Path, help="Candidate SQL file")
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT_PATH)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    parser.add_argument("--dialect", default="duckdb")
    parser.add_argument(
        "--repair",
        action="store_true",
        help="Apply one deterministic projection-removal repair attempt",
    )
    parser.add_argument(
        "--repair-output",
        type=Path,
        default=DEFAULT_REPAIR_PATH,
    )
    parser.add_argument("--report-output", type=Path)
    args = parser.parse_args()

    try:
        context = load_context(args.context)
        policy = load_policy(args.policy)
        sql = args.sql_file.read_text(encoding="utf-8")
        original = evaluate_governance(sql, context, policy, args.dialect)
        result: dict[str, Any] = {
            **original,
            "sql_file": display_path(args.sql_file),
            "context_file": display_path(args.context),
            "policy_file": display_path(args.policy),
            "dialect": args.dialect,
        }

        if args.repair and original["status"] == "FAIL":
            repaired_sql, repair = repair_sql(
                sql,
                original,
                context,
                policy,
                args.dialect,
            )
            repaired_result = evaluate_governance(
                repaired_sql,
                context,
                policy,
                args.dialect,
            )
            if repaired_result["status"] != "PASS":
                raise GovernanceError("Repaired SQL did not pass governance re-validation")
            args.repair_output.parent.mkdir(parents=True, exist_ok=True)
            args.repair_output.write_text(repaired_sql, encoding="utf-8")
            result = {
                "gate": "governance",
                "status": "REPAIRED",
                "policy_id": policy["policy_id"],
                "original_result": original,
                "repair": {
                    **repair,
                    "output_file": display_path(args.repair_output),
                },
                "repaired_result": repaired_result,
                "message": "Bounded repair completed and passed governance re-validation.",
            }
    except (OSError, GovernanceError, ValueError) as exc:
        result = {
            "gate": "governance",
            "status": "ERROR",
            "message": str(exc),
        }
        if args.report_output:
            write_json(args.report_output, result)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 2

    if args.report_output:
        write_json(args.report_output, result)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] in {"PASS", "REPAIRED"} else 1


if __name__ == "__main__":
    sys.exit(main())
