import argparse
import concurrent.futures
import datetime as dt
import decimal
import json
import math
import sys
from pathlib import Path
from typing import Any

import duckdb

from codegen import build_grounded_system_prompt, generate_sql
from governance_gate import (
    GovernanceError,
    evaluate_governance,
    load_policy,
)
from validator import ValidationError, render_dbt_relations, validate_sql


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "benchmarks" / "worlds.json"
DEFAULT_POLICY = PROJECT_ROOT / "policies" / "pii_direct_projection.json"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "benchmarks" / "results"
DEFAULT_SUMMARY_JSON = PROJECT_ROOT / "examples" / "benchmark_summary.json"
DEFAULT_SUMMARY_MD = PROJECT_ROOT / "examples" / "benchmark_results.md"
MODES = ("blind", "datahub_context", "context_assurance")


class BenchmarkError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchmarkError(f"File not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BenchmarkError(f"Expected a JSON object in {path}")
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, default=json_default) + "\n",
        encoding="utf-8",
    )


def json_default(value: Any) -> Any:
    if isinstance(value, (dt.date, dt.datetime, dt.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return float(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def load_benchmark(config_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    config = read_json(config_path)
    worlds = config.get("worlds")
    if not isinstance(worlds, list) or not worlds:
        raise BenchmarkError("Benchmark config must contain a non-empty worlds list")

    config_dir = config_path.parent
    total_tasks = 0
    for world in worlds:
        if not isinstance(world, dict):
            raise BenchmarkError("Every world must be an object")
        context_ref = world.get("context_file")
        if not isinstance(context_ref, str):
            raise BenchmarkError("Every world must have context_file")
        context_path = (config_dir / context_ref).resolve()
        context = read_json(context_path)
        fields = context.get("fields")
        rows = world.get("rows")
        tasks = world.get("tasks")
        if not isinstance(fields, list) or not fields:
            raise BenchmarkError(f"{world.get('id')}: context fields are missing")
        if not isinstance(rows, list):
            raise BenchmarkError(f"{world.get('id')}: rows must be a list")
        if any(not isinstance(row, list) or len(row) != len(fields) for row in rows):
            raise BenchmarkError(f"{world.get('id')}: row width does not match schema")
        if not isinstance(tasks, list) or not tasks:
            raise BenchmarkError(f"{world.get('id')}: tasks are missing")
        for task in tasks:
            if not all(isinstance(task.get(key), str) for key in ("id", "prompt", "reference_sql")):
                raise BenchmarkError(f"{world.get('id')}: invalid task")
        total_tasks += len(tasks)
        world["_context_path"] = context_path
        world["_context"] = context
    if total_tasks < 1:
        raise BenchmarkError("Benchmark has no tasks")
    return config, worlds


def build_blind_prompt(table_name: str) -> str:
    return f"""You are an expert analytics engineer writing DuckDB-compatible dbt SQL.

The only available metadata is the source table name `{table_name}`. You do not
have its schema, descriptions, governance tags, or lineage. Infer any source
columns you need from the table name and business task.

Query the source with {{{{ ref('{table_name}') }}}}.
Return SQL only, without Markdown fences or explanatory prose.
"""


def generate_initial(
    world: dict[str, Any], task: dict[str, Any], mode: str
) -> dict[str, Any]:
    context = world["_context"]
    prompt = (
        build_blind_prompt(context["table_name"])
        if mode == "blind"
        else build_grounded_system_prompt(context)
    )
    try:
        sql = generate_sql(task["prompt"], prompt)
    except Exception as exc:  # Record provider failures in the denominator.
        return {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    return {"status": "OK", "sql": sql.rstrip() + "\n"}


def quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def prepare_database(connection: duckdb.DuckDBPyConnection, world: dict[str, Any]) -> None:
    context = world["_context"]
    definitions = ", ".join(
        f"{quoted(field['fieldPath'])} {field.get('nativeDataType', 'VARCHAR')}"
        for field in context["fields"]
    )
    table = quoted(context["table_name"])
    connection.execute(f"CREATE TABLE {table} ({definitions})")
    rows = world["rows"]
    if rows:
        placeholders = ", ".join("?" for _ in context["fields"])
        connection.executemany(
            f"INSERT INTO {table} VALUES ({placeholders})",
            rows,
        )


def execute_sql(world: dict[str, Any], sql: str) -> dict[str, Any]:
    connection = duckdb.connect(":memory:")
    try:
        prepare_database(connection, world)
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
    }


def values_equal(left: Any, right: Any) -> bool:
    numeric = (int, float, decimal.Decimal)
    if isinstance(left, numeric) and isinstance(right, numeric):
        return math.isclose(float(left), float(right), rel_tol=1e-9, abs_tol=1e-6)
    if isinstance(left, (dt.date, dt.datetime)) and isinstance(right, (dt.date, dt.datetime)):
        return left.isoformat() == right.isoformat()
    return left == right


def compare_results(candidate: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    if candidate["status"] != "PASS":
        return {
            "gate": "result",
            "status": "SKIPPED",
            "message": "Candidate did not pass the runtime gate.",
        }
    candidate_columns = [name.casefold() for name in candidate["columns"]]
    expected_columns = [name.casefold() for name in expected["columns"]]
    issues: list[dict[str, Any]] = []
    if candidate_columns != expected_columns:
        issues.append(
            {
                "type": "column_contract_mismatch",
                "expected": expected["columns"],
                "actual": candidate["columns"],
            }
        )
    if len(candidate["rows"]) != len(expected["rows"]):
        issues.append(
            {
                "type": "row_count_mismatch",
                "expected": len(expected["rows"]),
                "actual": len(candidate["rows"]),
            }
        )
    elif not issues:
        for row_number, (actual_row, expected_row) in enumerate(
            zip(candidate["rows"], expected["rows"]), start=1
        ):
            if len(actual_row) != len(expected_row) or any(
                not values_equal(actual, wanted)
                for actual, wanted in zip(actual_row, expected_row)
            ):
                issues.append(
                    {
                        "type": "row_value_mismatch",
                        "row_number": row_number,
                        "expected": expected_row,
                        "actual": actual_row,
                    }
                )
                if len(issues) >= 3:
                    break
    return {
        "gate": "result",
        "status": "PASS" if not issues else "FAIL",
        "expected_columns": expected["columns"],
        "expected_row_count": len(expected["rows"]),
        "issues": issues,
        "message": (
            "Candidate output matches the reference result contract."
            if not issues
            else "Candidate output does not match the reference result contract."
        ),
    }


def evaluate_candidate(
    world: dict[str, Any],
    task: dict[str, Any],
    sql_path: Path,
    policy: dict[str, Any],
) -> dict[str, Any]:
    context = world["_context"]
    context_path = world["_context_path"]
    sql = sql_path.read_text(encoding="utf-8")
    try:
        schema = validate_sql(sql_path, context_path, "duckdb")
        schema["sql_file"] = relative(sql_path)
        schema["context_file"] = relative(context_path)
    except (ValidationError, OSError, ValueError) as exc:
        schema = {
            "gate": "schema",
            "status": "FAIL",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }

    if schema["status"] == "PASS":
        try:
            governance = evaluate_governance(sql, context, policy, "duckdb")
        except (GovernanceError, ValueError) as exc:
            governance = {
                "gate": "governance",
                "status": "FAIL",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
    else:
        governance = {
            "gate": "governance",
            "status": "SKIPPED",
            "message": "Schema gate did not pass.",
        }

    runtime = execute_sql(world, sql)
    expected = execute_sql(world, task["reference_sql"])
    if expected["status"] != "PASS":
        raise BenchmarkError(
            f"Reference SQL failed for {task['id']}: {expected.get('message')}"
        )
    result = compare_results(runtime, expected)
    gates = {
        "schema": schema,
        "runtime": runtime,
        "governance": governance,
        "result": result,
    }
    ready = all(item["status"] == "PASS" for item in gates.values())
    return {
        "verified_merge_ready": ready,
        "gates": gates,
    }


def build_failure_report(
    evaluation: dict[str, Any],
    world: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    for gate_name, gate in evaluation["gates"].items():
        if gate["status"] == "PASS":
            continue
        item: dict[str, Any] = {
            "gate": gate_name,
            "status": "BLOCK",
            "message": gate.get("message"),
        }
        if gate_name == "schema":
            item["violations"] = gate.get("invalid_details", [])
            item["allowed_actions"] = ["replace_with_allowed_field", "exclude"]
        elif gate_name == "governance":
            item["violations"] = gate.get("violations", [])
            item["allowed_actions"] = ["exclude"]
        elif gate_name == "runtime":
            item["error_type"] = gate.get("error_type")
            item["allowed_actions"] = ["fix_sql_without_changing_task"]
        elif gate_name == "result":
            item["issues"] = gate.get("issues", [])
            item["expected_columns"] = gate.get("expected_columns", [])
            item["expected_row_count"] = gate.get("expected_row_count")
            item["allowed_actions"] = ["preserve_task_semantics", "match_output_contract"]
        failures.append(item)
    return {
        "status": "BLOCK",
        "repair_attempts_allowed": 1,
        "table_name": world["_context"]["table_name"],
        "safe_result_contract": task.get("safe_result_contract"),
        "failures": failures,
    }


def repair_candidate(
    original_sql: str,
    failure_report: dict[str, Any],
    world: dict[str, Any],
    task: dict[str, Any],
) -> dict[str, Any]:
    system_prompt = build_grounded_system_prompt(world["_context"]) + """

You are the bounded repair stage of an automated SQL admission controller. You
have exactly one repair attempt. Correct every reported gate failure while
preserving unaffected business logic. For governance failures, never project a
PII field; use only non-sensitive fields that satisfy the safe result contract.
Return SQL only. If no safe repair exists, return a SQL comment beginning with
-- BLOCKED:.
"""
    repair_task = (
        f"Original business task:\n{task['prompt']}\n\n"
        "Structured failure report:\n"
        f"{json.dumps(failure_report, indent=2, ensure_ascii=False)}\n\n"
        f"Original SQL:\n{original_sql}\n\nProduce the single repaired SQL candidate."
    )
    try:
        sql = generate_sql(repair_task, system_prompt)
    except Exception as exc:
        return {
            "status": "ERROR",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    return {"status": "OK", "sql": sql.rstrip() + "\n"}


def empty_evaluation(message: str) -> dict[str, Any]:
    return {
        "verified_merge_ready": False,
        "gates": {
            name: {"gate": name, "status": "SKIPPED", "message": message}
            for name in ("schema", "runtime", "governance", "result")
        },
    }


def summarize(records: list[dict[str, Any]], benchmark_id: str, run_id: str) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "benchmark_id": benchmark_id,
        "run_id": run_id,
        "claim_scope": "Small directional benchmark; no zero-hallucination claim.",
        "metric": (
            "Verified Merge Readiness = generations passing schema, runtime, "
            "governance, and result gates / all generations"
        ),
        "modes": {},
    }
    for mode in MODES:
        mode_records = [record for record in records if record["mode"] == mode]
        ready = sum(record["evaluation"]["verified_merge_ready"] for record in mode_records)
        gates: dict[str, Any] = {}
        for gate_name in ("schema", "runtime", "governance", "result"):
            passed = sum(
                record["evaluation"]["gates"][gate_name]["status"] == "PASS"
                for record in mode_records
            )
            gates[gate_name] = {
                "passed": passed,
                "total": len(mode_records),
                "rate": passed / len(mode_records) if mode_records else 0.0,
            }
        by_world: dict[str, Any] = {}
        for world_id in sorted({record["world"] for record in mode_records}):
            subset = [record for record in mode_records if record["world"] == world_id]
            count = sum(record["evaluation"]["verified_merge_ready"] for record in subset)
            by_world[world_id] = {
                "passed": count,
                "total": len(subset),
                "rate": count / len(subset),
            }
        summary["modes"][mode] = {
            "verified_merge_ready": ready,
            "total": len(mode_records),
            "rate": ready / len(mode_records) if mode_records else 0.0,
            "gates": gates,
            "by_world": by_world,
            "repairs": {
                "attempted": sum(
                    record.get("repair", {}).get("attempts", 0)
                    for record in mode_records
                ),
                "passed": sum(
                    record.get("repair", {}).get("status") == "PASS"
                    for record in mode_records
                ),
            },
        }
    return summary


def summary_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Small Benchmark Results",
        "",
        f"Run: `{summary['run_id']}`",
        "",
        "> This is a small, controlled, directional benchmark. It does not claim that hallucinations were reduced to zero.",
        "",
        "Verified Merge Readiness requires one generation to pass all four deterministic gates: schema, DuckDB runtime, field-level governance, and reference-result equivalence.",
        "",
        "| Mode | Ready | VMR | Schema | Runtime | Governance | Result | Repairs |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for mode in MODES:
        item = summary["modes"][mode]
        gates = item["gates"]
        lines.append(
            "| {mode} | {ready}/{total} | {rate:.1%} | {schema:.1%} | "
            "{runtime:.1%} | {governance:.1%} | {result:.1%} | {repairs} |".format(
                mode=mode.replace("_", " ").title(),
                ready=item["verified_merge_ready"],
                total=item["total"],
                rate=item["rate"],
                schema=gates["schema"]["rate"],
                runtime=gates["runtime"]["rate"],
                governance=gates["governance"]["rate"],
                result=gates["result"]["rate"],
                repairs=(
                    f"{item['repairs']['passed']}/{item['repairs']['attempted']} passed"
                    if item["repairs"]["attempted"]
                    else "0"
                ),
            )
        )
    lines.extend(["", "## VMR by schema world", "", "| Mode | Familiar | Legacy | Governed |", "|---|---:|---:|---:|"])
    for mode in MODES:
        worlds = summary["modes"][mode]["by_world"]
        lines.append(
            "| {mode} | {f:.1%} | {l:.1%} | {g:.1%} |".format(
                mode=mode.replace("_", " ").title(),
                f=worlds.get("familiar", {}).get("rate", 0),
                l=worlds.get("legacy", {}).get("rate", 0),
                g=worlds.get("governed", {}).get("rate", 0),
            )
        )
    lines.extend(
        [
            "",
            "The Context + Assurance mode reuses the exact DataHub Context initial candidate. It permits at most one structured repair, so it does not gain an extra initial sample.",
            "",
        ]
    )
    return "\n".join(lines)


def publish_report(
    *,
    config: dict[str, Any],
    worlds: list[dict[str, Any]],
    records: list[dict[str, Any]],
    run_id: str,
    args: argparse.Namespace,
    rescored_from_existing_sql: bool = False,
) -> dict[str, Any]:
    run_dir = args.output_root / run_id
    summary = summarize(records, config["benchmark_id"], run_id)
    report = {
        "benchmark_id": config["benchmark_id"],
        "run_id": run_id,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "model": "z-ai/glm-5.2",
        "design": {
            "paired_context_and_assurance": True,
            "initial_generations_per_task": 2,
            "max_repair_attempts": 1,
            "world_count": len(worlds),
            "task_count": sum(len(world["tasks"]) for world in worlds),
            "gates": ["schema", "runtime", "governance", "result"],
            "rescored_from_existing_sql": rescored_from_existing_sql,
        },
        "summary": summary,
        "records": records,
    }
    run_metadata_path = run_dir / "run_metadata.json"
    if run_metadata_path.exists():
        report["run_metadata"] = read_json(run_metadata_path)
    write_json(run_dir / "report.json", report)
    write_json(args.output_root / "latest.json", report)
    write_json(args.summary_json, summary)
    args.summary_md.parent.mkdir(parents=True, exist_ok=True)
    args.summary_md.write_text(summary_markdown(summary), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
    return report


def rescore_existing(args: argparse.Namespace) -> dict[str, Any]:
    config, worlds = load_benchmark(args.config)
    policy = load_policy(args.policy)
    run_id = args.rescore_run
    run_dir = args.output_root / run_id
    sql_dir = run_dir / "sql"
    if not sql_dir.is_dir():
        raise BenchmarkError(f"Existing run SQL directory not found: {sql_dir}")

    records: list[dict[str, Any]] = []
    context_records: dict[tuple[str, str], dict[str, Any]] = {}
    for world in worlds:
        for task in world["tasks"]:
            for mode in ("blind", "datahub_context"):
                sql_path = sql_dir / f"{task['id']}__{mode}.sql"
                if sql_path.exists():
                    evaluation = evaluate_candidate(world, task, sql_path, policy)
                    generation = {"status": "OK"}
                    sql_file: str | None = relative(sql_path)
                else:
                    evaluation = empty_evaluation("Initial SQL file is missing.")
                    generation = {"status": "ERROR", "message": "Initial SQL file is missing."}
                    sql_file = None
                record = {
                    "world": world["id"],
                    "task_id": task["id"],
                    "task": task["prompt"],
                    "mode": mode,
                    "initial_generation": generation,
                    "sql_file": sql_file,
                    "repair": {
                        "bounded": True,
                        "max_attempts": 0,
                        "attempts": 0,
                        "status": "NOT_APPLICABLE",
                    },
                    "evaluation": evaluation,
                }
                records.append(record)
                if mode == "datahub_context":
                    context_records[(world["id"], task["id"])] = record

    for world in worlds:
        for task in world["tasks"]:
            initial = context_records[(world["id"], task["id"])]
            if initial["initial_generation"]["status"] != "OK":
                evaluation = empty_evaluation("Paired DataHub Context SQL is missing.")
                repair = {
                    "bounded": True,
                    "max_attempts": 1,
                    "attempts": 0,
                    "status": "UNAVAILABLE_NO_INITIAL_CANDIDATE",
                }
                final_sql_file = None
            elif initial["evaluation"]["verified_merge_ready"]:
                evaluation = initial["evaluation"]
                repair = {
                    "bounded": True,
                    "max_attempts": 1,
                    "attempts": 0,
                    "status": "NOT_NEEDED",
                }
                final_sql_file = initial["sql_file"]
            else:
                repaired_path = sql_dir / f"{task['id']}__context_assurance.sql"
                failure = build_failure_report(initial["evaluation"], world, task)
                if repaired_path.exists():
                    evaluation = evaluate_candidate(world, task, repaired_path, policy)
                    final_sql_file = relative(repaired_path)
                    repair = {
                        "bounded": True,
                        "max_attempts": 1,
                        "attempts": 1,
                        "status": "PASS" if evaluation["verified_merge_ready"] else "FAIL",
                        "structured_failure_report": failure,
                    }
                else:
                    evaluation = empty_evaluation("Existing bounded-repair SQL is missing.")
                    final_sql_file = initial["sql_file"]
                    repair = {
                        "bounded": True,
                        "max_attempts": 1,
                        "attempts": 1,
                        "status": "ERROR",
                        "structured_failure_report": failure,
                        "message": "Existing bounded-repair SQL is missing.",
                    }
            records.append(
                {
                    "world": world["id"],
                    "task_id": task["id"],
                    "task": task["prompt"],
                    "mode": "context_assurance",
                    "paired_initial_mode": "datahub_context",
                    "initial_sql_file": initial["sql_file"],
                    "sql_file": final_sql_file,
                    "repair": repair,
                    "evaluation": evaluation,
                }
            )

    return publish_report(
        config=config,
        worlds=worlds,
        records=records,
        run_id=run_id,
        args=args,
        rescored_from_existing_sql=True,
    )


def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    config, worlds = load_benchmark(args.config)
    policy = load_policy(args.policy)
    run_id = args.run_id or dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.output_root / run_id
    sql_dir = run_dir / "sql"
    sql_dir.mkdir(parents=True, exist_ok=False)

    jobs: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    for world in worlds:
        for task in world["tasks"]:
            for mode in ("blind", "datahub_context"):
                jobs.append((world, task, mode))

    generated: dict[tuple[str, str, str], dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {
            executor.submit(generate_initial, world, task, mode): (
                world["id"], task["id"], mode
            )
            for world, task, mode in jobs
        }
        for future in concurrent.futures.as_completed(future_map):
            key = future_map[future]
            try:
                generated[key] = future.result()
            except Exception as exc:
                generated[key] = {
                    "status": "ERROR",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            print(f"generated {key}: {generated[key]['status']}", flush=True)

    records: list[dict[str, Any]] = []
    context_records: dict[tuple[str, str], dict[str, Any]] = {}
    repair_jobs: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]] = []

    for world in worlds:
        for task in world["tasks"]:
            for mode in ("blind", "datahub_context"):
                key = (world["id"], task["id"], mode)
                generation = generated[key]
                sql_path = sql_dir / f"{task['id']}__{mode}.sql"
                if generation["status"] == "OK":
                    sql_path.write_text(generation["sql"], encoding="utf-8")
                    evaluation = evaluate_candidate(world, task, sql_path, policy)
                    sql_file: str | None = relative(sql_path)
                else:
                    evaluation = empty_evaluation("Initial model generation failed.")
                    sql_file = None
                record = {
                    "world": world["id"],
                    "task_id": task["id"],
                    "task": task["prompt"],
                    "mode": mode,
                    "initial_generation": generation if generation["status"] != "OK" else {"status": "OK"},
                    "sql_file": sql_file,
                    "repair": {"bounded": True, "max_attempts": 0, "attempts": 0, "status": "NOT_APPLICABLE"},
                    "evaluation": evaluation,
                }
                records.append(record)
                if mode == "datahub_context":
                    context_records[(world["id"], task["id"])] = record
                    if generation["status"] == "OK" and not evaluation["verified_merge_ready"]:
                        failure = build_failure_report(evaluation, world, task)
                        repair_jobs.append((world, task, failure, generation["sql"]))

    repaired: dict[tuple[str, str], dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        future_map = {
            executor.submit(repair_candidate, sql, failure, world, task): (
                world["id"], task["id"]
            )
            for world, task, failure, sql in repair_jobs
        }
        for future in concurrent.futures.as_completed(future_map):
            key = future_map[future]
            try:
                repaired[key] = future.result()
            except Exception as exc:
                repaired[key] = {
                    "status": "ERROR",
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            print(f"repaired {key}: {repaired[key]['status']}", flush=True)

    for world in worlds:
        for task in world["tasks"]:
            key = (world["id"], task["id"])
            initial = context_records[key]
            if initial["initial_generation"]["status"] != "OK":
                evaluation = empty_evaluation("Paired DataHub Context generation failed.")
                repair = {
                    "bounded": True,
                    "max_attempts": 1,
                    "attempts": 0,
                    "status": "UNAVAILABLE_NO_INITIAL_CANDIDATE",
                }
                final_sql_file = None
            elif initial["evaluation"]["verified_merge_ready"]:
                evaluation = initial["evaluation"]
                repair = {
                    "bounded": True,
                    "max_attempts": 1,
                    "attempts": 0,
                    "status": "NOT_NEEDED",
                }
                final_sql_file = initial["sql_file"]
            else:
                response = repaired[key]
                failure = build_failure_report(initial["evaluation"], world, task)
                if response["status"] == "OK":
                    repaired_path = sql_dir / f"{task['id']}__context_assurance.sql"
                    repaired_path.write_text(response["sql"], encoding="utf-8")
                    evaluation = evaluate_candidate(world, task, repaired_path, policy)
                    final_sql_file = relative(repaired_path)
                    repair = {
                        "bounded": True,
                        "max_attempts": 1,
                        "attempts": 1,
                        "status": "PASS" if evaluation["verified_merge_ready"] else "FAIL",
                        "structured_failure_report": failure,
                    }
                else:
                    evaluation = empty_evaluation("The single repair provider call failed.")
                    final_sql_file = initial["sql_file"]
                    repair = {
                        "bounded": True,
                        "max_attempts": 1,
                        "attempts": 1,
                        "status": "ERROR",
                        "structured_failure_report": failure,
                        "provider_error": response,
                    }
            records.append(
                {
                    "world": world["id"],
                    "task_id": task["id"],
                    "task": task["prompt"],
                    "mode": "context_assurance",
                    "paired_initial_mode": "datahub_context",
                    "initial_sql_file": initial["sql_file"],
                    "sql_file": final_sql_file,
                    "repair": repair,
                    "evaluation": evaluation,
                }
            )

    return publish_report(
        config=config,
        worlds=worlds,
        records=records,
        run_id=run_id,
        args=args,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the paired small Verified Merge Readiness benchmark."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--summary-json", type=Path, default=DEFAULT_SUMMARY_JSON)
    parser.add_argument("--summary-md", type=Path, default=DEFAULT_SUMMARY_MD)
    parser.add_argument("--run-id")
    parser.add_argument(
        "--rescore-run",
        help="Re-evaluate an existing run's SQL without making any model calls.",
    )
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 8:
        parser.error("--workers must be between 1 and 8")
    return args


def main() -> int:
    try:
        args = parse_args()
        if args.rescore_run:
            rescore_existing(args)
        else:
            run_benchmark(args)
    except (BenchmarkError, OSError, RuntimeError) as exc:
        print(f"benchmark error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
