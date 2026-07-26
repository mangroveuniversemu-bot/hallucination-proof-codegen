import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


SCHEMA_PAGE_SIZE = 5
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


def select_dataset(search_json: dict[str, Any]) -> dict[str, Any]:
    """Use the same dbt dataset filtering rule proven in test_mcp.py."""
    for item in search_json.get("searchResults", []):
        entity = item.get("entity", {})
        urn = entity.get("urn", "")
        if urn.startswith("urn:li:dataset:") and "dbt" in urn:
            return entity
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


async def build_context(table_keyword: str) -> dict[str, Any]:
    server_params = StdioServerParameters(
        command="mcp-server-datahub",
        args=[],
    )

    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            search_result = await session.call_tool(
                "search",
                {"query": table_keyword, "num_results": 5},
            )
            search_json = parse_tool_json(search_result, "search")
            target_entity = select_dataset(search_json)
            target_urn = target_entity["urn"]

            fields = await fetch_all_schema_fields(session, target_urn)

            lineage_result = await session.call_tool(
                "get_lineage",
                {"urn": target_urn, "upstream": True, "max_hops": 1},
            )
            lineage_json = parse_tool_json(lineage_result, "get_lineage")

    entity_name = target_entity.get("name", table_keyword)
    table_name = str(entity_name).rsplit(".", maxsplit=1)[-1]
    return {
        "table_name": table_name,
        "urn": target_urn,
        "fields": fields,
        "upstream_tables": extract_upstream_tables(lineage_json),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a schema and upstream-lineage context bundle from DataHub."
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
