import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

# Keep local governance setup deterministic when outbound telemetry is blocked.
os.environ.setdefault("DATAHUB_TELEMETRY_ENABLED", "false")

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig
from datahub.metadata.schema_classes import TagPropertiesClass
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from context_builder import fetch_all_schema_fields
from governance_gate import field_classifications


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTEXT_PATH = PROJECT_ROOT / "examples" / "context_bundle.json"
DEFAULT_GMS_URL = "http://localhost:8080"
DEFAULT_TAG_URN = "urn:li:tag:PII"
DEFAULT_COLUMNS = ["first_name", "last_name"]

load_dotenv(PROJECT_ROOT / ".env")


class BootstrapError(RuntimeError):
    pass


def load_context(path: Path) -> dict[str, Any]:
    try:
        context = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BootstrapError(f"Context file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BootstrapError(f"Context file contains invalid JSON: {path}") from exc
    if not isinstance(context, dict):
        raise BootstrapError("Context must contain a JSON object")
    urn = context.get("urn")
    fields = context.get("fields")
    if not isinstance(urn, str) or not urn.startswith("urn:li:dataset:"):
        raise BootstrapError("Context is missing a valid dataset urn")
    if not isinstance(fields, list):
        raise BootstrapError("Context is missing fields")
    return context


def ensure_tag_exists(gms_url: str, token: str | None, tag_urn: str) -> bool:
    graph = DataHubGraph(DatahubClientConfig(server=gms_url, token=token))
    if graph.exists(tag_urn):
        return False
    emitter = DatahubRestEmitter(gms_server=gms_url, token=token)
    emitter.test_connection()
    emitter.emit_mcp(
        MetadataChangeProposalWrapper(
            entityUrn=tag_urn,
            aspect=TagPropertiesClass(
                name=tag_urn.rsplit(":", maxsplit=1)[-1],
                description=(
                    "Personally identifiable information. Used by the "
                    "hallucination-proof codegen governance gate."
                ),
            ),
        )
    )
    return True


def field_has_tag(field: dict[str, Any], tag_urn: str) -> bool:
    expected = tag_urn.casefold()
    return any(
        token.casefold() == expected or token.casefold() == "pii"
        for token in field_classifications(field)
    )


async def apply_and_verify(
    *,
    context_path: Path,
    gms_url: str,
    token: str | None,
    tag_urn: str,
    columns: list[str],
    server_command: str,
    server_args: list[str],
) -> dict[str, Any]:
    context = load_context(context_path)
    urn = context["urn"]
    available = {
        field.get("fieldPath")
        for field in context["fields"]
        if isinstance(field, dict)
    }
    missing_fields = sorted(set(columns) - available)
    if missing_fields:
        raise BootstrapError(
            f"Requested governance fields are not in context: {missing_fields}"
        )

    created_tag = ensure_tag_exists(gms_url, token, tag_urn)
    server_env = os.environ.copy()
    server_env["DATAHUB_GMS_URL"] = gms_url
    server_env["TOOLS_IS_MUTATION_ENABLED"] = "true"
    server_env["DATAHUB_TELEMETRY_ENABLED"] = "false"
    if token:
        server_env["DATAHUB_GMS_TOKEN"] = token

    params = StdioServerParameters(
        command=server_command,
        args=server_args,
        env=server_env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {tool.name for tool in tools.tools}
            if "add_tags" not in tool_names:
                raise BootstrapError(
                    "add_tags is unavailable; mutation tools were not enabled"
                )

            before_fields = await fetch_all_schema_fields(session, urn)
            by_name = {
                field.get("fieldPath"): field
                for field in before_fields
                if isinstance(field, dict)
            }
            columns_to_add = [
                column
                for column in columns
                if not field_has_tag(by_name.get(column, {}), tag_urn)
            ]

            if columns_to_add:
                # DataHub Core 1.5 only persisted the last subresource when the
                # same dataset URN appeared multiple times in one batch. Keep
                # each field mutation independent and verify all fields below.
                for column in columns_to_add:
                    result = await session.call_tool(
                        "add_tags",
                        {
                            "tag_urns": [tag_urn],
                            "entity_urns": [urn],
                            "column_paths": [column],
                        },
                    )
                    if getattr(result, "isError", False):
                        raise BootstrapError(
                            f"DataHub MCP add_tags returned an error for {column}"
                        )

            verified_fields = await fetch_all_schema_fields(session, urn)

    verified_by_name = {
        field.get("fieldPath"): field
        for field in verified_fields
        if isinstance(field, dict)
    }
    unverified = [
        column
        for column in columns
        if not field_has_tag(verified_by_name.get(column, {}), tag_urn)
    ]
    if unverified:
        raise BootstrapError(f"PII tag read-back failed for: {unverified}")

    context["fields"] = verified_fields
    context_path.write_text(
        json.dumps(context, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "SUCCESS",
        "dataset_urn": urn,
        "tag_urn": tag_urn,
        "tag_created": created_tag,
        "columns_requested": columns,
        "columns_added": columns_to_add,
        "columns_verified": columns,
        "context_file": str(context_path),
        "message": "Field-level PII tags were applied and read back through DataHub MCP.",
    }


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply and verify field-level PII tags through DataHub MCP."
    )
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT_PATH)
    parser.add_argument(
        "--gms-url",
        default=os.environ.get("DATAHUB_GMS_URL", DEFAULT_GMS_URL),
    )
    parser.add_argument(
        "--tag-urn",
        default=os.environ.get("DATAHUB_PII_TAG_URN", DEFAULT_TAG_URN),
    )
    parser.add_argument(
        "--column",
        action="append",
        dest="columns",
        help="Column to classify; repeat as needed",
    )
    parser.add_argument(
        "--server-command",
        default=os.environ.get("DATAHUB_MCP_COMMAND", "mcp-server-datahub"),
    )
    parser.add_argument(
        "--server-arg",
        action="append",
        default=[],
        dest="server_args",
    )
    args = parser.parse_args()

    try:
        result = await apply_and_verify(
            context_path=args.context,
            gms_url=args.gms_url.rstrip("/"),
            token=os.environ.get("DATAHUB_GMS_TOKEN") or None,
            tag_urn=args.tag_urn,
            columns=args.columns or DEFAULT_COLUMNS,
            server_command=args.server_command,
            server_args=args.server_args,
        )
    except (OSError, ValueError, BootstrapError) as exc:
        print(
            json.dumps(
                {"status": "ERROR", "message": str(exc)},
                indent=2,
                ensure_ascii=False,
            )
        )
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
