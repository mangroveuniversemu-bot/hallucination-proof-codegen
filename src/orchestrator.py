import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codegen import (
    BASE_URL,
    GENERATION_TEMPERATURE,
    MODEL,
    generate_sql,
)
from governance_gate import GovernanceError, evaluate_governance, load_context, load_policy
from result_gate import (
    ResultGateError,
    evaluate_result_contract,
    execute_duckdb,
    load_contract,
    public_runtime_report,
)
from validator import ValidationError, validate_sql
from writeback import DEFAULT_ACTOR, append_institutional_memory


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTEXT = PROJECT_ROOT / "examples" / "context_bundle.json"
DEFAULT_POLICY = PROJECT_ROOT / "policies" / "pii_direct_projection.json"
DEFAULT_TASK = PROJECT_ROOT / "demo" / "task.txt"
DEFAULT_CONTRACT = PROJECT_ROOT / "demo" / "result_contract.json"
DEFAULT_DATABASE = PROJECT_ROOT.parent / "jaffle_shop_duckdb" / "jaffle_shop.duckdb"
DEFAULT_RUNS_ROOT = PROJECT_ROOT / "runs"
DEFAULT_GMS_URL = "http://localhost:8080"
REPOSITORY_URL = "https://github.com/mangroveuniversemu-bot/hallucination-proof-codegen"
PROMPT_CONTRACT_VERSION = "fair_context_ablation_v1"


SYSTEM_PROMPT_TEMPLATE = """You are an expert analytics engineer writing DuckDB-compatible dbt SQL.

Source model: {table_name}

<DATAHUB_CONTEXT>
{context_block}
</DATAHUB_CONTEXT>

The DATAHUB_CONTEXT block is the only experimental variable. The business task,
model, decoding parameters, source model, and all other instructions are held
constant. If the block says AVAILABLE, treat its field list and governance
metadata as complete and authoritative: never invent a source column. If it
says UNAVAILABLE, infer the source columns needed to attempt the task.

Query the source with {{{{ ref('{table_name}') }}}}. Derived expressions and
output aliases are allowed. Return SQL only, without Markdown fences or prose.
"""


class OrchestratorError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_new_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)


def write_new_json(path: Path, value: Any) -> None:
    write_new_text(
        path,
        json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n",
    )


def portable_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return path.name


def git_snapshot() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise OrchestratorError(f"Unable to capture git state: {exc}") from exc
    return {
        "commit": commit,
        "dirty": bool(status),
        "dirty_path_count": len(status),
    }


def dependency_versions() -> dict[str, str]:
    packages = (
        "acryl-datahub",
        "duckdb",
        "httpx",
        "mcp",
        "mcp-server-datahub",
        "openai",
        "python-dotenv",
        "sqlglot",
    )
    versions: dict[str, str] = {}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "NOT_INSTALLED"
    return versions


def context_block(context: dict[str, Any] | None, table_name: str) -> str:
    if context is None:
        return json.dumps(
            {
                "status": "UNAVAILABLE",
                "table_name": table_name,
                "fields": None,
                "governance": None,
                "lineage": None,
            },
            indent=2,
        )
    return json.dumps(
        {
            "status": "AVAILABLE",
            "table_name": context["table_name"],
            "urn": context["urn"],
            "fields": context["fields"],
            "upstream_tables": context.get("upstream_tables", []),
        },
        indent=2,
        ensure_ascii=False,
    )


def build_fair_system_prompt(
    *, context: dict[str, Any] | None, table_name: str
) -> str:
    return SYSTEM_PROMPT_TEMPLATE.format(
        table_name=table_name,
        context_block=context_block(context, table_name),
    )


def normalize_schema_report(
    report: dict[str, Any], sql_path: Path, context_path: Path
) -> dict[str, Any]:
    result = dict(report)
    result["sql_file"] = portable_path(sql_path)
    result["context_file"] = portable_path(context_path)
    result["gate"] = "schema"
    return result


def evaluate_candidate(
    *,
    sql_path: Path,
    context_path: Path,
    context: dict[str, Any],
    policy: dict[str, Any],
    contract: dict[str, Any],
    database_path: Path,
) -> dict[str, Any]:
    sql = sql_path.read_text(encoding="utf-8")
    try:
        schema = normalize_schema_report(
            validate_sql(sql_path, context_path, "duckdb"),
            sql_path,
            context_path,
        )
    except (OSError, ValidationError, ValueError) as exc:
        schema = {
            "gate": "schema",
            "status": "FAIL",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }

    try:
        execution = execute_duckdb(sql, database_path)
    except (OSError, ResultGateError, ValueError) as exc:
        execution = {
            "gate": "runtime",
            "status": "FAIL",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
    runtime = public_runtime_report(execution)
    result_quality = evaluate_result_contract(execution, contract)

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

    gates = {
        "schema": schema,
        "runtime": runtime,
        "result_quality": result_quality,
        "governance": governance,
    }
    return {
        "all_gates_pass": all(gate["status"] == "PASS" for gate in gates.values()),
        "gates": gates,
    }


def structured_failure_report(
    evaluation: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    actions_by_classification = policy.get("allowed_actions_by_classification", {})
    for gate_name, gate in evaluation["gates"].items():
        if gate["status"] == "PASS":
            continue
        failure: dict[str, Any] = {
            "gate": gate_name,
            "status": "BLOCK",
            "message": gate.get("message"),
        }
        if gate_name == "schema":
            failure["violations"] = gate.get("invalid_details", [])
            failure["allowed_actions"] = ["replace_with_allowed_field", "exclude"]
        elif gate_name == "runtime":
            failure["error_type"] = gate.get("error_type")
            failure["allowed_actions"] = ["fix_sql_without_changing_task"]
        elif gate_name == "result_quality":
            failure["violations"] = gate.get("violations", [])
        elif gate_name == "governance":
            violations: list[dict[str, Any]] = []
            for violation in gate.get("violations", []):
                allowed: list[str] = []
                for classification in violation.get("matched_classifications", []):
                    key = classification.rsplit(":", maxsplit=1)[-1].upper()
                    allowed.extend(actions_by_classification.get(key, []))
                violations.append({**violation, "allowed_actions": list(dict.fromkeys(allowed))})
            failure["violations"] = violations
        failures.append(failure)
    return {
        "status": "BLOCK",
        "repair_attempts_allowed": 1,
        "failures": failures,
    }


def build_repair_prompt(
    *,
    task: str,
    original_sql: str,
    failure_report: dict[str, Any],
    contract: dict[str, Any],
) -> str:
    return (
        f"Original business task:\n{task}\n\n"
        "Safe result contract:\n"
        f"{json.dumps(contract, indent=2, ensure_ascii=False)}\n\n"
        "Structured gate failure report:\n"
        f"{json.dumps(failure_report, indent=2, ensure_ascii=False)}\n\n"
        f"Original SQL:\n{original_sql}\n\n"
        "You have exactly one repair attempt. Apply only the allowed actions in "
        "the failure report, preserve unaffected business logic, and satisfy the "
        "required safe result contract. Return SQL only."
    )


def manifest_file_entries(run_dir: Path) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for path in sorted(item for item in run_dir.rglob("*") if item.is_file()):
        if path.name == "manifest.json":
            continue
        relative = path.relative_to(run_dir).as_posix()
        entries[relative] = {
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }
    return entries


def seal_run(
    *,
    run_dir: Path,
    run_id: str,
    created_at: str,
    git: dict[str, Any],
    task_sha256: str,
    prompt_template_sha256: str,
    blind_prompt_sha256: str,
    grounded_prompt_sha256: str,
    status: str,
) -> dict[str, Any]:
    lock_path = PROJECT_ROOT / "requirements.lock"
    manifest_base: dict[str, Any] = {
        "manifest_version": 1,
        "immutable": True,
        "run_id": run_id,
        "created_at_utc": created_at,
        "status": status,
        "source": git,
        "provider": {
            "model": MODEL,
            "base_url": BASE_URL,
            "temperature": GENERATION_TEMPERATURE,
        },
        "fairness": {
            "contract_version": PROMPT_CONTRACT_VERSION,
            "same_user_prompt_for_blind_and_grounded": True,
            "user_prompt_sha256": task_sha256,
            "system_prompt_template_sha256": prompt_template_sha256,
            "blind_system_prompt_sha256": blind_prompt_sha256,
            "grounded_system_prompt_sha256": grounded_prompt_sha256,
            "only_experimental_variable": "DATAHUB_CONTEXT block",
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "dependencies": dependency_versions(),
            "requirements_lock_sha256": sha256_file(lock_path) if lock_path.exists() else None,
        },
        "files": manifest_file_entries(run_dir),
    }
    manifest_base["manifest_sha256"] = sha256_bytes(canonical_json(manifest_base))
    manifest_path = run_dir / "manifest.json"
    write_new_json(manifest_path, manifest_base)
    for path in (item for item in run_dir.rglob("*") if item.is_file()):
        path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    return manifest_base


def verify_run(run_dir: Path) -> dict[str, Any]:
    manifest_path = run_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OrchestratorError(f"Manifest not found: {manifest_path}") from exc
    expected_manifest_hash = manifest.get("manifest_sha256")
    unsigned = dict(manifest)
    unsigned.pop("manifest_sha256", None)
    actual_manifest_hash = sha256_bytes(canonical_json(unsigned))
    violations: list[dict[str, Any]] = []
    if expected_manifest_hash != actual_manifest_hash:
        violations.append(
            {
                "type": "manifest_digest_mismatch",
                "expected": expected_manifest_hash,
                "actual": actual_manifest_hash,
            }
        )

    expected_files = manifest.get("files", {})
    actual_paths = {
        path.relative_to(run_dir).as_posix()
        for path in run_dir.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    expected_paths = set(expected_files)
    for missing in sorted(expected_paths - actual_paths):
        violations.append({"type": "missing_file", "file": missing})
    for extra in sorted(actual_paths - expected_paths):
        violations.append({"type": "unexpected_file", "file": extra})
    for relative in sorted(expected_paths & actual_paths):
        actual_hash = sha256_file(run_dir / relative)
        expected_hash = expected_files[relative]["sha256"]
        if actual_hash != expected_hash:
            violations.append(
                {
                    "type": "file_digest_mismatch",
                    "file": relative,
                    "expected": expected_hash,
                    "actual": actual_hash,
                }
            )
    return {
        "status": "PASS" if not violations else "FAIL",
        "run_id": manifest.get("run_id"),
        "manifest_sha256": actual_manifest_hash,
        "verified_file_count": len(expected_paths),
        "violations": violations,
    }


def gate_statuses(evaluation: dict[str, Any]) -> dict[str, str]:
    return {name: gate["status"] for name, gate in evaluation["gates"].items()}


def run(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    task = args.task_file.read_text(encoding="utf-8").strip()
    if not task:
        raise OrchestratorError("Task file is empty")
    context = load_context(args.context)
    policy = load_policy(args.policy)
    contract = load_contract(args.contract)
    table_name = context["table_name"]
    created_at = datetime.now(timezone.utc).isoformat()
    run_id = args.run_id or (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + uuid.uuid4().hex[:8]
    )
    git = git_snapshot()
    if args.require_clean_git and git["dirty"]:
        raise OrchestratorError(
            "Git worktree is dirty; commit the implementation before creating an immutable demo run"
        )
    final_run_dir = args.runs_root / run_id
    if final_run_dir.exists():
        raise OrchestratorError(f"Run already exists and cannot be overwritten: {final_run_dir}")
    pending_root = args.runs_root / ".pending"
    pending_root.mkdir(parents=True, exist_ok=True)
    run_dir = pending_root / run_id
    run_dir.mkdir(exist_ok=False)

    blind_system = build_fair_system_prompt(context=None, table_name=table_name)
    grounded_system = build_fair_system_prompt(context=context, table_name=table_name)
    write_new_text(run_dir / "inputs" / "task.txt", task + "\n")
    write_new_text(run_dir / "inputs" / "system_prompt_template.txt", SYSTEM_PROMPT_TEMPLATE)
    write_new_text(run_dir / "inputs" / "blind_system_prompt.txt", blind_system)
    write_new_text(run_dir / "inputs" / "grounded_system_prompt.txt", grounded_system)
    write_new_text(run_dir / "inputs" / "context.json", args.context.read_text(encoding="utf-8"))
    write_new_text(run_dir / "inputs" / "governance_policy.json", args.policy.read_text(encoding="utf-8"))
    write_new_text(run_dir / "inputs" / "result_contract.json", args.contract.read_text(encoding="utf-8"))

    blind_sql = generate_sql(
        task,
        blind_system,
        temperature=GENERATION_TEMPERATURE,
    )
    grounded_sql = generate_sql(
        task,
        grounded_system,
        temperature=GENERATION_TEMPERATURE,
    )
    blind_path = run_dir / "sql" / "blind.sql"
    grounded_path = run_dir / "sql" / "grounded_initial.sql"
    write_new_text(blind_path, blind_sql.rstrip() + "\n")
    write_new_text(grounded_path, grounded_sql.rstrip() + "\n")

    blind_evaluation = evaluate_candidate(
        sql_path=blind_path,
        context_path=args.context,
        context=context,
        policy=policy,
        contract=contract,
        database_path=args.database,
    )
    grounded_initial = evaluate_candidate(
        sql_path=grounded_path,
        context_path=args.context,
        context=context,
        policy=policy,
        contract=contract,
        database_path=args.database,
    )

    repair: dict[str, Any] = {
        "bounded": True,
        "max_attempts": 1,
        "attempts": 0,
        "status": "NOT_NEEDED",
    }
    final_evaluation = grounded_initial
    final_sql_path = grounded_path
    failure_report: dict[str, Any] | None = None
    if not grounded_initial["all_gates_pass"]:
        failure_report = structured_failure_report(grounded_initial, policy)
        repair["attempts"] = 1
        repair["status"] = "ATTEMPTED"
        repair_system = (
            grounded_system
            + "\nYou are the bounded repair stage. You have exactly one repair attempt.\n"
        )
        repair_task = build_repair_prompt(
            task=task,
            original_sql=grounded_sql,
            failure_report=failure_report,
            contract=contract,
        )
        repaired_sql = generate_sql(
            repair_task,
            repair_system,
            temperature=GENERATION_TEMPERATURE,
        )
        final_sql_path = run_dir / "sql" / "repaired.sql"
        write_new_text(final_sql_path, repaired_sql.rstrip() + "\n")
        final_evaluation = evaluate_candidate(
            sql_path=final_sql_path,
            context_path=args.context,
            context=context,
            policy=policy,
            contract=contract,
            database_path=args.database,
        )
        repair["status"] = "PASS" if final_evaluation["all_gates_pass"] else "FAIL"

    writeback_receipt: dict[str, Any] = {
        "status": "NOT_REQUESTED",
        "added": False,
    }
    if args.writeback:
        if not final_evaluation["all_gates_pass"]:
            writeback_receipt = {
                "status": "BLOCKED",
                "added": False,
                "message": "Writeback requires all final assurance gates to pass.",
            }
        else:
            evidence_url = args.evidence_url or f"{REPOSITORY_URL}/tree/main/runs/{run_id}"
            writeback_receipt = append_institutional_memory(
                urn=context["urn"],
                task=task,
                note=(
                    f"Immutable assurance run {run_id}: schema, runtime, NULL/result, "
                    f"and governance gates passed after {repair['attempts']} repair attempt(s)."
                ),
                evidence_url=evidence_url,
                gms_url=args.gms_url,
                token=os.environ.get("DATAHUB_GMS_TOKEN"),
                actor=args.actor,
            )
    write_new_json(run_dir / "writeback_receipt.json", writeback_receipt)

    overall_pass = final_evaluation["all_gates_pass"] and (
        not args.writeback or writeback_receipt.get("status") in {"SUCCESS", "UNCHANGED"}
    )
    report = {
        "orchestrator": "hallucination_proof_demo_v1",
        "run_id": run_id,
        "status": "PASS" if overall_pass else "BLOCK",
        "task": task,
        "fairness": {
            "same_user_prompt": True,
            "same_model": True,
            "same_temperature": True,
            "only_experimental_variable": "DATAHUB_CONTEXT block",
        },
        "blind": blind_evaluation,
        "grounded_initial": grounded_initial,
        "structured_failure_report": failure_report,
        "repair": repair,
        "final": final_evaluation,
        "final_sql_file": final_sql_path.relative_to(run_dir).as_posix(),
        "writeback": writeback_receipt,
    }
    write_new_json(run_dir / "gate_report.json", report)
    manifest = seal_run(
        run_dir=run_dir,
        run_id=run_id,
        created_at=created_at,
        git=git,
        task_sha256=sha256_bytes(task.encode("utf-8")),
        prompt_template_sha256=sha256_bytes(SYSTEM_PROMPT_TEMPLATE.encode("utf-8")),
        blind_prompt_sha256=sha256_bytes(blind_system.encode("utf-8")),
        grounded_prompt_sha256=sha256_bytes(grounded_system.encode("utf-8")),
        status=report["status"],
    )
    report["manifest_sha256"] = manifest["manifest_sha256"]
    run_dir.rename(final_run_dir)
    return report, final_run_dir


def print_run_summary(report: dict[str, Any], run_dir: Path) -> None:
    print(f"Run: {report['run_id']}")
    print(f"Blind gates: {json.dumps(gate_statuses(report['blind']))}")
    print(f"Grounded initial: {json.dumps(gate_statuses(report['grounded_initial']))}")
    print(
        f"Repair: attempts={report['repair']['attempts']} "
        f"status={report['repair']['status']}"
    )
    print(f"Final gates: {json.dumps(gate_statuses(report['final']))}")
    null_quality = report["final"]["gates"]["result_quality"].get("null_quality", {})
    total_nulls = sum(item.get("null_count", 0) for item in null_quality.values())
    print(f"Final NULL violations: {total_nulls}")
    print(f"Writeback: {report['writeback'].get('status')}")
    print(f"Manifest: {report['manifest_sha256']}")
    print(f"Artifacts: {run_dir}")
    print(f"Overall: {report['status']}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run or verify the fair, bounded, metadata-grounded SQL demo."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--task-file", type=Path, default=DEFAULT_TASK)
    run_parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT)
    run_parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    run_parser.add_argument("--contract", type=Path, default=DEFAULT_CONTRACT)
    run_parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    run_parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    run_parser.add_argument("--run-id")
    run_parser.add_argument("--writeback", action="store_true")
    run_parser.add_argument("--evidence-url")
    run_parser.add_argument("--gms-url", default=DEFAULT_GMS_URL)
    run_parser.add_argument("--actor", default=DEFAULT_ACTOR)
    run_parser.add_argument("--require-clean-git", action="store_true")
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("run_dir", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "verify":
        try:
            result = verify_run(args.run_dir)
        except (OSError, OrchestratorError, ValueError) as exc:
            result = {"status": "ERROR", "message": str(exc)}
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 2
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["status"] == "PASS" else 1

    try:
        report, run_dir = run(args)
    except Exception as exc:
        print(f"orchestrator error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print_run_summary(report, run_dir)
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
