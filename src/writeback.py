import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig
from datahub.metadata.schema_classes import (
    AuditStampClass,
    DatasetPropertiesClass,
    InstitutionalMemoryClass,
    InstitutionalMemoryMetadataClass,
)
from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTEXT_PATH = PROJECT_ROOT / "examples" / "context_bundle.json"
DEFAULT_GMS_URL = "http://localhost:8080"
DEFAULT_UI_URL = "http://localhost:9002"
DEFAULT_EVIDENCE_URL = (
    "https://github.com/mangroveuniversemu-bot/"
    "hallucination-proof-codegen/blob/main/examples/output_grounded.sql"
)
DEFAULT_ACTOR = "urn:li:corpuser:datahub"

load_dotenv(PROJECT_ROOT / ".env")


class WritebackError(RuntimeError):
    pass


def load_dataset_context(context_path: Path) -> tuple[str, str]:
    try:
        context = json.loads(context_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WritebackError(f"Context file not found: {context_path}") from exc
    except json.JSONDecodeError as exc:
        raise WritebackError(f"Context file contains invalid JSON: {context_path}") from exc

    if not isinstance(context, dict):
        raise WritebackError("Context must contain a JSON object")

    urn = context.get("urn")
    table_name = context.get("table_name")
    if not isinstance(urn, str) or not urn.startswith("urn:li:dataset:"):
        raise WritebackError("Context is missing a valid dataset urn")
    if not isinstance(table_name, str) or not table_name:
        raise WritebackError("Context is missing a non-empty table_name")
    return urn, table_name


def build_description(task: str, note: str, generated_at: datetime) -> str:
    timestamp = generated_at.astimezone(timezone.utc).isoformat(timespec="seconds")
    return (
        "Agent-generated SQL evidence. "
        f"Generated from the DataHub schema at {timestamp}. "
        f"Task: {task.strip()} "
        f"Note: {note.strip()}"
    )


def description_matches(
    element: InstitutionalMemoryMetadataClass,
    evidence_url: str,
    task: str,
    note: str,
) -> bool:
    return (
        element.url == evidence_url
        and f"Task: {task.strip()}" in element.description
        and f"Note: {note.strip()}" in element.description
    )


def append_institutional_memory(
    *,
    urn: str,
    task: str,
    note: str,
    evidence_url: str,
    gms_url: str,
    token: str | None,
    actor: str,
) -> dict[str, Any]:
    client_config = DatahubClientConfig(server=gms_url, token=token)
    graph = DataHubGraph(client_config)
    emitter = DatahubRestEmitter(gms_server=gms_url, token=token)
    emitter.test_connection()

    if not graph.exists(urn):
        raise WritebackError(f"Dataset does not exist in DataHub: {urn}")

    dataset_properties_before = graph.get_aspect(urn, DatasetPropertiesClass)
    original_description = (
        dataset_properties_before.description if dataset_properties_before else None
    )

    current_memory = graph.get_aspect(urn, InstitutionalMemoryClass)
    existing_elements = list(current_memory.elements) if current_memory else []

    for element in existing_elements:
        if description_matches(element, evidence_url, task, note):
            return {
                "status": "UNCHANGED",
                "added": False,
                "urn": urn,
                "aspect": "institutionalMemory",
                "element_count": len(existing_elements),
                "description": element.description,
                "evidence_url": evidence_url,
                "dataset_description_preserved": True,
                "message": "Matching institutional-memory entry already exists.",
            }

    now = datetime.now(timezone.utc)
    audit_stamp = AuditStampClass(
        time=int(now.timestamp() * 1000),
        actor=actor,
    )
    description = build_description(task, note, now)
    new_element = InstitutionalMemoryMetadataClass(
        url=evidence_url,
        description=description,
        createStamp=audit_stamp,
    )
    updated_memory = InstitutionalMemoryClass(
        elements=[*existing_elements, new_element]
    )
    proposal = MetadataChangeProposalWrapper(
        entityUrn=urn,
        aspect=updated_memory,
    )
    emitter.emit_mcp(proposal)

    verified_memory: InstitutionalMemoryClass | None = None
    for _attempt in range(5):
        verified_memory = graph.get_aspect(urn, InstitutionalMemoryClass)
        if verified_memory and any(
            element.url == evidence_url and element.description == description
            for element in verified_memory.elements
        ):
            break
        time.sleep(1)
    else:
        raise WritebackError(
            "The emitter returned, but the institutional-memory entry could not be read back"
        )

    dataset_properties_after = graph.get_aspect(urn, DatasetPropertiesClass)
    resulting_description = (
        dataset_properties_after.description if dataset_properties_after else None
    )
    description_preserved = original_description == resulting_description
    if not description_preserved:
        raise WritebackError("Dataset description changed unexpectedly during writeback")

    return {
        "status": "SUCCESS",
        "added": True,
        "urn": urn,
        "aspect": "institutionalMemory",
        "element_count": len(verified_memory.elements),
        "description": description,
        "evidence_url": evidence_url,
        "dataset_description_preserved": description_preserved,
        "message": "Institutional-memory entry was emitted and read back successfully.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Append agent-generation evidence to a DataHub dataset."
    )
    parser.add_argument("note", help="Human-readable writeback note")
    parser.add_argument("--task", required=True, help="Original SQL generation task")
    parser.add_argument(
        "--context",
        type=Path,
        default=DEFAULT_CONTEXT_PATH,
        help=f"Context bundle path (default: {DEFAULT_CONTEXT_PATH})",
    )
    parser.add_argument(
        "--urn",
        help="Optional dataset URN override; defaults to the context bundle urn",
    )
    parser.add_argument(
        "--evidence-url",
        default=DEFAULT_EVIDENCE_URL,
        help="URL displayed with the DataHub institutional-memory entry",
    )
    parser.add_argument(
        "--gms-url",
        default=os.environ.get("DATAHUB_GMS_URL", DEFAULT_GMS_URL),
        help="DataHub GMS URL",
    )
    parser.add_argument(
        "--ui-url",
        default=os.environ.get("DATAHUB_UI_URL", DEFAULT_UI_URL),
        help="DataHub UI base URL used for the verification link",
    )
    parser.add_argument(
        "--actor",
        default=os.environ.get("DATAHUB_ACTOR", DEFAULT_ACTOR),
        help="DataHub actor URN recorded in the audit stamp",
    )
    args = parser.parse_args()

    try:
        context_urn, table_name = load_dataset_context(args.context)
        urn = args.urn or context_urn
        result = append_institutional_memory(
            urn=urn,
            task=args.task,
            note=args.note,
            evidence_url=args.evidence_url,
            gms_url=args.gms_url.rstrip("/"),
            token=os.environ.get("DATAHUB_GMS_TOKEN") or None,
            actor=args.actor,
        )
    except (OSError, WritebackError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "ERROR", "message": str(exc)},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 1

    encoded_urn = quote(urn, safe="")
    result["table_name"] = table_name
    result["datahub_ui_url"] = (
        f"{args.ui_url.rstrip('/')}/dataset/{encoded_urn}"
    )
    result["ui_verification"] = (
        f"Open DataHub, search for {table_name}, and inspect the Links section."
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
