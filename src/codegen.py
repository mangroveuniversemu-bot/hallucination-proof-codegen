import argparse
import json
import os
import re
import ssl
from pathlib import Path
from typing import Any

import certifi
import httpx
from dotenv import load_dotenv
from openai import OpenAI


MODEL = "z-ai/glm-5.2"
BASE_URL = "https://integrate.api.nvidia.com/v1"
GENERATION_TEMPERATURE = 0.0
BLIND_TABLE_NAME = "customers"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONTEXT_PATH = PROJECT_ROOT / "examples" / "context_bundle.json"
GROUNDED_OUTPUT_PATH = PROJECT_ROOT / "examples" / "output_grounded.sql"
BLIND_OUTPUT_PATH = PROJECT_ROOT / "examples" / "output_blind.sql"

load_dotenv(PROJECT_ROOT / ".env")


def load_context() -> dict[str, Any]:
    try:
        context = json.loads(CONTEXT_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Context file not found: {CONTEXT_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Context file contains invalid JSON: {CONTEXT_PATH}") from exc

    if not isinstance(context, dict):
        raise RuntimeError("context_bundle.json must contain a JSON object")
    if not context.get("table_name") or not context.get("urn"):
        raise RuntimeError("Context is missing table_name or urn")
    if not isinstance(context.get("fields"), list) or not context["fields"]:
        raise RuntimeError("Context must contain a non-empty fields list")
    return context


def format_fields(fields: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for field in fields:
        field_path = field.get("fieldPath")
        native_type = field.get("nativeDataType", "UNKNOWN")
        description = field.get("description") or "No description provided."
        nullable = field.get("nullable")
        nullable_text = "unknown"
        if nullable is True:
            nullable_text = "yes"
        elif nullable is False:
            nullable_text = "no"

        governance_labels: list[str] = []
        for metadata_key in (
            "tags",
            "globalTags",
            "editedTags",
            "glossaryTerms",
            "editedGlossaryTerms",
        ):
            metadata = field.get(metadata_key)
            if isinstance(metadata, str):
                governance_labels.append(metadata)
            elif isinstance(metadata, list):
                governance_labels.extend(
                    item for item in metadata if isinstance(item, str)
                )
            elif isinstance(metadata, dict):
                nested = metadata.get("tags") or metadata.get("terms") or []
                for item in nested:
                    if isinstance(item, str):
                        governance_labels.append(item)
                    elif isinstance(item, dict):
                        label = item.get("tag") or item.get("term") or item.get("name")
                        if isinstance(label, str):
                            governance_labels.append(label)

        governance_text = ", ".join(dict.fromkeys(governance_labels)) or "none"

        if not isinstance(field_path, str) or not field_path:
            raise RuntimeError("Every field must have a non-empty fieldPath")
        lines.append(
            f"- `{field_path}` | type: {native_type} | nullable: {nullable_text} "
            f"| governance: {governance_text} | description: {description}"
        )
    return "\n".join(lines)


def build_grounded_system_prompt(context: dict[str, Any]) -> str:
    upstream_tables = context.get("upstream_tables", [])
    upstream_text = ", ".join(upstream_tables) if upstream_tables else "none listed"

    return f"""You are an expert analytics engineer writing DuckDB-compatible dbt SQL.

The following DataHub metadata is the complete and authoritative context for the
source model in this task.

Table name: {context['table_name']}
Dataset URN: {context['urn']}
Immediate upstream tables: {upstream_text}

Allowed source columns:
{format_fields(context['fields'])}

Rules you must follow:
1. Every source-column reference must exactly match a fieldPath in the allowed
   source-column list above. Never invent or assume a source column.
2. In particular, do not substitute plausible names such as `total_spent` when
   the requested concept is already represented by an allowed column.
3. Query the source model with {{{{ ref('{context['table_name']}') }}}}.
4. Derived expressions and new output aliases are allowed, but they must be
   calculated only from allowed source columns.
5. If the task cannot be completed from the available columns, return a SQL
   comment explaining which information is missing instead of inventing fields.
6. Return SQL only, without Markdown fences or explanatory prose.
"""


def build_blind_system_prompt() -> str:
    return f"""You are an expert analytics engineer writing DuckDB-compatible dbt SQL.

The only available information is that the table is named `{BLIND_TABLE_NAME}`.
No schema, column list, descriptions, or lineage metadata are available. Infer
which columns the table probably contains and write the requested SQL based on
your own judgment.

Query the table with {{{{ ref('{BLIND_TABLE_NAME}') }}}}.
Return SQL only, without Markdown fences or explanatory prose.
"""


def strip_markdown_fence(content: str) -> str:
    content = content.strip()
    fenced = re.fullmatch(
        r"```(?:sql)?\s*(.*?)\s*```",
        content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if fenced:
        return fenced.group(1).strip()
    return content


def build_ssl_context() -> ssl.SSLContext:
    """Trust certifi plus Windows certificate stores without disabling TLS checks."""
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    if os.name == "nt" and hasattr(ssl, "enum_certificates"):
        for store_name in ("ROOT", "CA"):
            for certificate, encoding, _trust in ssl.enum_certificates(store_name):
                if encoding == "x509_asn":
                    pem = ssl.DER_cert_to_PEM_cert(certificate)
                    ssl_context.load_verify_locations(cadata=pem)
    return ssl_context


def generate_sql(
    task: str,
    system_prompt: str,
    *,
    temperature: float = GENERATION_TEMPERATURE,
) -> str:
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        raise RuntimeError(
            "NVIDIA_API_KEY is not set. In PowerShell, set it for this session "
            "with: $env:NVIDIA_API_KEY = '<your API key>'"
        )

    with httpx.Client(verify=build_ssl_context()) as http_client:
        client = OpenAI(
            api_key=api_key,
            base_url=BASE_URL,
            http_client=http_client,
        )
        response = client.chat.completions.create(
            model=MODEL,
            temperature=temperature,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ],
        )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("GLM returned an empty response")
    return strip_markdown_fence(content)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate grounded or blind dbt SQL with GLM."
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="Natural-language SQL task. If omitted, you will be prompted for it.",
    )
    parser.add_argument(
        "--blind",
        action="store_true",
        help="Generate without loading DataHub context and write output_blind.sql.",
    )
    args = parser.parse_args()

    task = args.task or input("Task description: ").strip()
    if not task:
        parser.error("Task description cannot be empty")

    if args.blind:
        system_prompt = build_blind_system_prompt()
        output_path = BLIND_OUTPUT_PATH
    else:
        context = load_context()
        system_prompt = build_grounded_system_prompt(context)
        output_path = GROUNDED_OUTPUT_PATH

    sql = generate_sql(task, system_prompt)
    print(sql)
    output_path.write_text(sql.rstrip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
