import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlglot import exp, parse
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import Scope, traverse_scope


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTEXT_PATH = PROJECT_ROOT / "examples" / "context_bundle.json"
DBT_REF_PATTERN = re.compile(
    r"\{\{\s*ref\(\s*['\"](?P<name>[^'\"]+)['\"]\s*\)\s*\}\}"
)
DBT_SOURCE_PATTERN = re.compile(
    r"\{\{\s*source\(\s*['\"][^'\"]+['\"]\s*,\s*"
    r"['\"](?P<name>[^'\"]+)['\"]\s*\)\s*\}\}"
)


class ValidationError(RuntimeError):
    pass


def load_context(context_path: Path) -> tuple[str, list[str]]:
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"Context file not found: {context_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Context file contains invalid JSON: {context_path}") from exc

    if not isinstance(context, dict):
        raise ValidationError("Context must contain a JSON object")

    table_name = context.get("table_name")
    fields = context.get("fields")
    if not isinstance(table_name, str) or not table_name:
        raise ValidationError("Context is missing a non-empty table_name")
    if not isinstance(fields, list) or not fields:
        raise ValidationError("Context is missing a non-empty fields list")

    field_paths: list[str] = []
    for field in fields:
        field_path = field.get("fieldPath") if isinstance(field, dict) else None
        if not isinstance(field_path, str) or not field_path:
            raise ValidationError("Every context field must have a non-empty fieldPath")
        field_paths.append(field_path)

    return table_name, field_paths


def render_dbt_relations(sql: str) -> str:
    """Replace dbt ref/source expressions with parseable table identifiers."""
    rendered = DBT_REF_PATTERN.sub(lambda match: match.group("name"), sql)
    rendered = DBT_SOURCE_PATTERN.sub(lambda match: match.group("name"), rendered)
    if "{{" in rendered or "{%" in rendered:
        raise ValidationError("Unsupported dbt/Jinja expression remains in the SQL")
    return rendered


def scope_output_names(
    scope: Scope,
    visited: set[int] | None = None,
) -> set[str]:
    """Resolve explicit outputs plus columns forwarded by derived-source stars."""
    visited = set() if visited is None else visited
    scope_id = id(scope)
    if scope_id in visited:
        return set()
    visited.add(scope_id)

    named_selects = getattr(scope.expression, "named_selects", [])
    outputs = {
        name.casefold()
        for name in named_selects
        if isinstance(name, str) and name and name != "*"
    }
    selects = getattr(scope.expression, "selects", [])
    star_selects = [item for item in selects if item.is_star]
    if not star_selects:
        return outputs

    try:
        sources = {
            alias.casefold(): source
            for alias, (_node, source) in scope.selected_sources.items()
        }
    except Exception as exc:
        raise ValidationError(f"Unable to expand derived SELECT *: {exc}") from exc

    for star in star_selects:
        qualifier = star.table.casefold() if getattr(star, "table", "") else ""
        candidates = [sources.get(qualifier)] if qualifier else list(sources.values())
        for source in candidates:
            if isinstance(source, Scope):
                outputs.update(scope_output_names(source, visited))
    return outputs


def local_select_aliases(scope: Scope) -> set[str]:
    selects = getattr(scope.expression, "selects", [])
    return {
        expression.alias.casefold()
        for expression in selects
        if isinstance(expression, exp.Alias) and expression.alias
    }


def add_reference(references: dict[str, str], name: str) -> None:
    references.setdefault(name.casefold(), name)


def analyze_references(expressions: list[exp.Expression]) -> dict[str, Any]:
    source_columns: dict[str, str] = {}
    derived_columns: dict[str, str] = {}
    physical_tables: dict[str, str] = {}
    invalid_reasons: dict[str, set[str]] = defaultdict(set)

    for expression in expressions:
        for scope in traverse_scope(expression):
            try:
                sources = {
                    alias.casefold(): source
                    for alias, (_node, source) in scope.selected_sources.items()
                }
            except Exception as exc:
                raise ValidationError(f"Unable to resolve SQL sources: {exc}") from exc

            for source in sources.values():
                if isinstance(source, exp.Table):
                    add_reference(physical_tables, source.name)

            explicit_aliases = local_select_aliases(scope)

            for column in scope.columns:
                if column.is_star or not column.name:
                    continue

                name = column.name
                key = name.casefold()
                qualifier = column.table.casefold() if column.table else ""

                if qualifier:
                    source = sources.get(qualifier)
                    if isinstance(source, Scope):
                        if key in scope_output_names(source):
                            add_reference(derived_columns, name)
                        else:
                            invalid_reasons[key].add("not_produced_by_derived_source")
                        continue

                    add_reference(source_columns, name)
                    continue

                alias_context = column.find_ancestor(
                    exp.Order,
                    exp.Group,
                    exp.Having,
                    exp.Qualify,
                )
                if key in explicit_aliases and alias_context is not None:
                    add_reference(derived_columns, name)
                    continue

                derived_sources = [
                    source for source in sources.values() if isinstance(source, Scope)
                ]
                physical_sources = [
                    source for source in sources.values() if isinstance(source, exp.Table)
                ]
                derived_matches = [
                    source
                    for source in derived_sources
                    if key in scope_output_names(source)
                ]

                if len(sources) == 1 and derived_sources:
                    if derived_matches:
                        add_reference(derived_columns, name)
                    else:
                        invalid_reasons[key].add("not_produced_by_derived_source")
                    continue

                if derived_matches and not physical_sources:
                    add_reference(derived_columns, name)
                    continue

                if physical_sources and not derived_matches:
                    add_reference(source_columns, name)
                    continue

                if derived_matches and physical_sources:
                    invalid_reasons[key].add("ambiguous_between_physical_and_derived_sources")
                    continue

                if derived_sources and not physical_sources:
                    invalid_reasons[key].add("not_produced_by_derived_source")
                    continue

                add_reference(source_columns, name)

    return {
        "source_columns": source_columns,
        "derived_columns": derived_columns,
        "physical_tables": physical_tables,
        "invalid_reasons": invalid_reasons,
    }


def validate_sql(sql_path: Path, context_path: Path, dialect: str) -> dict[str, Any]:
    table_name, allowed_fields = load_context(context_path)
    allowed_lookup = {field.casefold(): field for field in allowed_fields}

    try:
        raw_sql = sql_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValidationError(f"SQL file not found: {sql_path}") from exc

    rendered_sql = render_dbt_relations(raw_sql)
    try:
        expressions = [
            expression
            for expression in parse(rendered_sql, read=dialect)
            if expression is not None
        ]
    except ParseError as exc:
        raise ValidationError(f"SQLGlot could not parse the SQL: {exc}") from exc
    if not expressions:
        raise ValidationError("SQL file contains no statements")

    analysis = analyze_references(expressions)
    source_columns: dict[str, str] = analysis["source_columns"]
    invalid_reasons: dict[str, set[str]] = analysis["invalid_reasons"]

    valid_columns: list[str] = []
    for key, display_name in source_columns.items():
        if key in allowed_lookup:
            valid_columns.append(allowed_lookup[key])
        else:
            invalid_reasons[key].add("not_in_context_schema")

    physical_tables: dict[str, str] = analysis["physical_tables"]
    unexpected_tables = sorted(
        name
        for key, name in physical_tables.items()
        if key != table_name.casefold()
    )

    hallucinated_columns = sorted(
        source_columns.get(key, key) for key in invalid_reasons
    )
    invalid_details = [
        {
            "column": source_columns.get(key, key),
            "reasons": sorted(reasons),
        }
        for key, reasons in sorted(invalid_reasons.items())
    ]
    status = "PASS" if not hallucinated_columns and not unexpected_tables else "FAIL"

    return {
        "status": status,
        "sql_file": str(sql_path.resolve()),
        "context_file": str(context_path.resolve()),
        "dialect": dialect,
        "table_name": table_name,
        "source_tables": sorted(physical_tables.values()),
        "allowed_columns": sorted(allowed_fields),
        "referenced_source_columns": sorted(source_columns.values()),
        "derived_columns": sorted(analysis["derived_columns"].values()),
        "valid_columns": sorted(valid_columns),
        "hallucinated_columns": hallucinated_columns,
        "unexpected_tables": unexpected_tables,
        "invalid_details": invalid_details,
        "message": (
            "All referenced source columns are valid."
            if status == "PASS"
            else "One or more SQL references are not supported by the DataHub context."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate SQL column references against a DataHub context bundle."
    )
    parser.add_argument("sql_file", type=Path, help="SQL file to validate")
    parser.add_argument(
        "--context",
        type=Path,
        default=DEFAULT_CONTEXT_PATH,
        help=f"Context bundle path (default: {DEFAULT_CONTEXT_PATH})",
    )
    parser.add_argument(
        "--dialect",
        default="duckdb",
        help="SQLGlot input dialect (default: duckdb)",
    )
    args = parser.parse_args()

    try:
        result = validate_sql(args.sql_file, args.context, args.dialect)
    except (OSError, ValidationError) as exc:
        result = {
            "status": "ERROR",
            "sql_file": str(args.sql_file),
            "context_file": str(args.context),
            "message": str(exc),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 2

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
