import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

os.environ.setdefault("DATAHUB_TELEMETRY_ENABLED", "false")

from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.emitter.rest_emitter import DatahubRestEmitter
from datahub.ingestion.graph.client import DataHubGraph
from datahub.ingestion.graph.config import DatahubClientConfig
from datahub.metadata.schema_classes import (
    DatasetLineageTypeClass,
    DatasetPropertiesClass,
    GlobalTagsClass,
    StatusClass,
    TagAssociationClass,
    TagPropertiesClass,
    UpstreamClass,
    UpstreamLineageClass,
)
from dotenv import load_dotenv

from context_builder import build_context


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTEXT_PATH = PROJECT_ROOT / "examples" / "context_bundle.json"
DEFAULT_GMS_URL = "http://localhost:8080"
DEMO_ASSETS = [
    {
        "name": "jaffle_shop.analytics.customer_value_dashboard",
        "criticality": "LOW",
        "description": "Demo consumer: customer value dashboard dataset.",
    },
    {
        "name": "jaffle_shop.analytics.monthly_revenue_report",
        "criticality": "MEDIUM",
        "description": "Demo consumer: reviewed monthly revenue reporting dataset.",
    },
    {
        "name": "jaffle_shop.analytics.churn_feature_table",
        "criticality": "HIGH",
        "description": "Demo consumer: production churn-model feature dataset.",
    },
]

load_dotenv(PROJECT_ROOT / ".env")


class ImpactBootstrapError(RuntimeError):
    pass


def dataset_urn(name: str) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:dbt,{name},PROD)"


def tag_urn(criticality: str) -> str:
    return f"urn:li:tag:CRITICALITY_{criticality}"


def load_source(path: Path) -> dict[str, Any]:
    try:
        context = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImpactBootstrapError(f"Could not load context: {path}") from exc
    urn = context.get("urn")
    if not isinstance(urn, str) or not urn.startswith("urn:li:dataset:"):
        raise ImpactBootstrapError("Context is missing a valid source dataset urn")
    return context


def emit_demo_graph(
    source_urn: str,
    gms_url: str,
    token: str | None,
) -> dict[str, Any]:
    emitter = DatahubRestEmitter(gms_server=gms_url, token=token)
    emitter.test_connection()
    graph = DataHubGraph(DatahubClientConfig(server=gms_url, token=token))
    created_tags: list[str] = []
    emitted_assets: list[dict[str, str]] = []

    for level in ("LOW", "MEDIUM", "HIGH"):
        urn = tag_urn(level)
        if not graph.exists(urn):
            emitter.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=urn,
                    aspect=TagPropertiesClass(
                        name=f"CRITICALITY_{level}",
                        description=(
                            f"{level} downstream change criticality for the "
                            "hallucination-proof codegen admission demo."
                        ),
                    ),
                )
            )
            created_tags.append(urn)

    for asset in DEMO_ASSETS:
        urn = dataset_urn(asset["name"])
        level = asset["criticality"]
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=DatasetPropertiesClass(
                    name=asset["name"].rsplit(".", maxsplit=1)[-1],
                    qualifiedName=asset["name"],
                    description=asset["description"],
                    customProperties={
                        "demo_managed_by": "hallucination-proof-codegen",
                        "criticality": level,
                    },
                ),
            )
        )
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=StatusClass(removed=False),
            )
        )
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=GlobalTagsClass(
                    tags=[TagAssociationClass(tag=tag_urn(level))]
                ),
            )
        )
        emitter.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=urn,
                aspect=UpstreamLineageClass(
                    upstreams=[
                        UpstreamClass(
                            dataset=source_urn,
                            type=DatasetLineageTypeClass.TRANSFORMED,
                        )
                    ]
                ),
            )
        )
        emitted_assets.append(
            {"urn": urn, "name": asset["name"], "criticality": level}
        )

    return {"created_tags": created_tags, "emitted_assets": emitted_assets}


async def apply_and_verify(
    context_path: Path,
    gms_url: str,
    token: str | None,
    server_command: str,
) -> dict[str, Any]:
    source = load_source(context_path)
    emitted = emit_demo_graph(source["urn"], gms_url, token)
    previous_command = os.environ.get("DATAHUB_MCP_COMMAND")
    os.environ["DATAHUB_MCP_COMMAND"] = server_command
    os.environ["DATAHUB_GMS_URL"] = gms_url
    try:
        refreshed = await build_context(source.get("table_name", "customers"))
    finally:
        if previous_command is None:
            os.environ.pop("DATAHUB_MCP_COMMAND", None)
        else:
            os.environ["DATAHUB_MCP_COMMAND"] = previous_command

    by_urn = {
        asset.get("urn"): asset
        for asset in refreshed.get("downstream_assets", [])
        if isinstance(asset, dict)
    }
    missing: list[str] = []
    mismatched: list[dict[str, Any]] = []
    for expected in emitted["emitted_assets"]:
        actual = by_urn.get(expected["urn"])
        if not actual:
            missing.append(expected["urn"])
            continue
        if actual.get("criticality") != expected["criticality"]:
            mismatched.append(
                {
                    "urn": expected["urn"],
                    "expected": expected["criticality"],
                    "actual": actual.get("criticality"),
                }
            )
        path = actual.get("lineage_path", [])
        if not path or path[0].get("urn") != source["urn"] or path[-1].get(
            "urn"
        ) != expected["urn"]:
            mismatched.append(
                {"urn": expected["urn"], "expected": "verified lineage path"}
            )
    if missing or mismatched:
        raise ImpactBootstrapError(
            f"MCP verification failed; missing={missing}, mismatched={mismatched}"
        )

    context_path.write_text(
        json.dumps(refreshed, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return {
        "status": "SUCCESS",
        **emitted,
        "verified_assets": [
            {
                "urn": expected["urn"],
                "criticality": by_urn[expected["urn"]]["criticality"],
                "degree": by_urn[expected["urn"]].get("degree"),
                "path_length": len(by_urn[expected["urn"]]["lineage_path"]),
            }
            for expected in emitted["emitted_assets"]
        ],
        "context_file": context_path.as_posix(),
        "message": "Demo downstream assets, criticality tags, and exact paths were read back through MCP.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create and verify a local DataHub downstream-impact demo graph."
    )
    parser.add_argument("--context", type=Path, default=DEFAULT_CONTEXT_PATH)
    parser.add_argument(
        "--gms-url",
        default=os.environ.get("DATAHUB_GMS_URL", DEFAULT_GMS_URL),
    )
    parser.add_argument(
        "--server-command",
        default=os.environ.get("DATAHUB_MCP_COMMAND", "mcp-server-datahub"),
    )
    args = parser.parse_args()
    try:
        result = asyncio.run(
            apply_and_verify(
                args.context,
                args.gms_url,
                os.environ.get("DATAHUB_GMS_TOKEN") or None,
                args.server_command,
            )
        )
    except (OSError, ImpactBootstrapError, RuntimeError) as exc:
        result = {"status": "ERROR", "message": str(exc)}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
