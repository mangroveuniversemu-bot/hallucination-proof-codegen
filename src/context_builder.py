import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SCHEMA_PAGE_SIZE = 5
LINEAGE_MAX_HOPS = 3
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = PROJECT_ROOT / "examples" / "context_bundle.json"


def parse_tool_json(tool_result: Any, tool_name: str) -> dict[str, Any]:
    """Extract a JSON object from an MCP tool result."""
    text_parts = [
        block.text
        for block in tool_result.content
        if getattr(block, "type", None) == "text"
    ]
    if not text_parts:
        raise RuntimeError(f"{tool_name} returned no text content")

    try:
        payload = json.loads("\n".join(text_parts))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{tool_name} returned invalid JSON") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"{tool_name} returned JSON that is not an object")
    return payload


def select_dataset(
    search_json: dict[str, Any],
    table_keyword: str | None = None,
) -> dict[str, Any]:
    """Prefer an exact dbt dataset name, then fall back to the proven filter."""
    candidates: list[dict[str, Any]] = []
    for item in search_json.get("searchResults", []):
        entity = item.get("entity", {})
        urn = entity.get("urn", "")
        if urn.startswith("urn:li:dataset:") and "dbt" in urn:
            candidates.append(entity)
    if table_keyword:
        expected = table_keyword.casefold()
        for entity in candidates:
            name = entity.get("name")
            if isinstance(name, str) and name.rsplit(".", maxsplit=1)[-1].casefold() == expected:
                return entity
    if candidates:
        return candidates[0]
    raise RuntimeError("No matching dbt dataset URN was found")


async def fetch_all_schema_fields(
    session: ClientSession,
    urn: str,
) -> list[dict[str, Any]]:
    """Fetch every schema page, advancing with the returned offset/count."""
    fields: list[dict[str, Any]] = []
    offset = 0

    while True:
        result = await session.call_tool(
            "list_schema_fields",
            {"urn": urn, "limit": SCHEMA_PAGE_SIZE, "offset": offset},
        )
        page = parse_tool_json(result, "list_schema_fields")
        page_fields = page.get("fields", [])
        if not isinstance(page_fields, list):
            raise RuntimeError("list_schema_fields returned a non-list 'fields' value")
        fields.extend(page_fields)

        remaining = page.get("remainingCount", 0)
        try:
            remaining_count = int(remaining or 0)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "list_schema_fields returned an invalid remainingCount"
            ) from exc

        if remaining_count <= 0:
            break

        returned = page.get("returned", len(page_fields))
        try:
            returned_count = int(returned)
            page_offset = int(page.get("offset", offset))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                "list_schema_fields returned invalid pagination metadata"
            ) from exc

        next_offset = page_offset + returned_count
        if returned_count <= 0 or next_offset <= offset:
            raise RuntimeError(
                "list_schema_fields pagination did not advance while fields remain"
            )
        offset = next_offset

    return fields


def extract_upstream_tables(lineage_json: dict[str, Any]) -> list[str]:
    upstreams = lineage_json.get("upstreams", {})
    table_names: list[str] = []

    for item in upstreams.get("searchResults", []):
        entity = item.get("entity", {})
        name = entity.get("name")
        if isinstance(name, str) and name:
            short_name = name.rsplit(".", maxsplit=1)[-1]
            if short_name not in table_names:
                table_names.append(short_name)

    return table_names


def _metadata_tokens(value: Any) -> list[str]:
    tokens: list[str] = []
    if isinstance(value, str):
        tokens.append(value)
    elif isinstance(value, list):
        for item in value:
            tokens.extend(_metadata_tokens(item))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"tags", "tag", "urn", "name", "properties"}:
                tokens.extend(_metadata_tokens(item))
    return list(dict.fromkeys(tokens))


def extract_criticality(entity: dict[str, Any]) -> tuple[str | None, list[str]]:
    classifications = _metadata_tokens(entity.get("tags", {}))
    normalized = [token.upper().replace("-", "_") for token in classifications]
    for level in ("HIGH", "MEDIUM", "LOW"):
        if any(
            f"CRITICALITY_{level}" in token
            or f"CRITICALITY:{level}" in token
            for token in normalized
        ):
            return level, classifications
    return None, classifications


def extract_downstream_assets(
    lineage_json: dict[str, Any],
    source_name: str,
) -> list[dict[str, Any]]:
    downstreams = lineage_json.get("downstreams", {})
    assets: list[dict[str, Any]] = []
    for item in downstreams.get("searchResults", []):
        entity = item.get("entity", {})
        urn = entity.get("urn")
        name = entity.get("name")
        if not isinstance(urn, str) or not isinstance(name, str):
            continue
        criticality, classifications = extract_criticality(entity)
        platform = entity.get("platform", {})
        assets.append(
            {
                "urn": urn,
                "name": name,
                "type": entity.get("type", "UNKNOWN"),
                "platform": platform.get("name") if isinstance(platform, dict) else None,
                "degree": item.get("degree"),
                "criticality": criticality,
                "classifications": classifications,
                "is_representation": (
                    name.rsplit(".", maxsplit=1)[-1].casefold()
                    == source_name.rsplit(".", maxsplit=1)[-1].casefold()
                ),
            }
        )
    return assets


def extract_path(path_json: dict[str, Any]) -> list[dict[str, str]]:
    paths = path_json.get("paths", [])
    if not paths:
        return []
    path = paths[0].get("path", [])
    return [
        {"urn": item["urn"], "type": item.get("type", "UNKNOWN")}
        for item in path
        if isinstance(item, dict) and isinstance(item.get("urn"), str)
    ]


async def build_context(table_keyword: str) -> dict[str, Any]:
    server_env = os.environ.copy()
    server_env["DATAHUB_TELEMETRY_ENABLED"] = "false"
    server_params = StdioServerParameters(
        command=os.environ.get("DATAHUB_MCP_COMMAND", "mcp-server-datahub"),
        args=[],
        env=server_env,
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            search_result = await session.call_tool(
                "search",
                {"query": table_keyword, "num_results": 5},
            )
            search_json = parse_tool_json(search_result, "search")
            target_entity = select_dataset(search_json, table_keyword)
            target_urn = target_entity["urn"]

            fields = await fetch_all_schema_fields(session, target_urn)

            lineage_result = await session.call_tool(
                "get_lineage",
                {"urn": target_urn, "upstream": True, "max_hops": 1},
            )
            lineage_json = parse_tool_json(lineage_result, "get_lineage")

            downstream_result = await session.call_tool(
                "get_lineage",
                {
                    "urn": target_urn,
                    "upstream": False,
                    "max_hops": LINEAGE_MAX_HOPS,
                    "max_results": 100,
                },
            )
            downstream_json = parse_tool_json(downstream_result, "get_lineage")
            source_name = str(target_entity.get("name", table_keyword))
            downstream_assets = extract_downstream_assets(
                downstream_json,
                source_name,
            )
            for asset in downstream_assets:
                path_result = await session.call_tool(
                    "get_lineage_paths_between",
                    {
                        "source_urn": target_urn,
                        "target_urn": asset["urn"],
                        "direction": "downstream",
                    },
                )
                path_json = parse_tool_json(
                    path_result,
                    "get_lineage_paths_between",
                )
                asset["lineage_path"] = extract_path(path_json)

    entity_name = target_entity.get("name", table_keyword)
    table_name = str(entity_name).rsplit(".", maxsplit=1)[-1]
    return {
        "table_name": table_name,
        "urn": target_urn,
        "fields": fields,
        "upstream_tables": extract_upstream_tables(lineage_json),
        "downstream_assets": downstream_assets,
        "lineage_max_hops": LINEAGE_MAX_HOPS,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build schema plus upstream/downstream lineage context from DataHub."
        )
    )
    parser.add_argument(
        "table_keyword",
        nargs="?",
        default="customers",
        help="Table-name search keyword (default: customers)",
    )
    args = parser.parse_args()

    context = await build_context(args.table_keyword)
    rendered = json.dumps(context, indent=2, ensure_ascii=False)
    print(rendered)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(rendered + "\n", encoding="utf-8")
    print(f"\nWrote {OUTPUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
