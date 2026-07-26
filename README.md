# Hallucination-Proof SQL Codegen

Generate dbt SQL with and without DataHub context, then compare the results.
The grounded path retrieves a real dataset schema and one-hop upstream lineage
through DataHub MCP before prompting `z-ai/glm-5.2`. The blind path receives only
the table name, which makes schema hallucinations directly observable.

## What this repository contains

```text
hallucination-proof-codegen/
├── src/
│   ├── context_builder.py
│   └── codegen.py
├── examples/
│   ├── context_bundle.json
│   ├── output_blind.sql
│   ├── blind_runtime_error.txt
│   ├── output_grounded.sql
│   ├── grounded_runtime_output.txt
│   ├── task.txt
│   └── README.md
├── .env.example
├── .gitignore
├── LICENSE
└── requirements.txt
```

The checked-in evidence uses the same business task and model in both modes.
The blind SQL invents `clv` and fails in DuckDB, while the grounded SQL uses the
DataHub field `customer_lifetime_value` and executes successfully. See
[`examples/README.md`](examples/README.md) for the raw evidence and reproduction
commands.

## External test dataset

This repository intentionally does **not** vendor the dbt Labs jaffle shop
project. The demo metadata was produced with
[`dbt-labs/jaffle_shop_duckdb`](https://github.com/dbt-labs/jaffle_shop_duckdb),
which should be cloned separately next to this repository:

```powershell
git clone https://github.com/dbt-labs/jaffle_shop_duckdb.git ..\jaffle_shop_duckdb
cd ..\jaffle_shop_duckdb
dbt build
dbt docs generate
```

Ingest the resulting DuckDB/dbt metadata into a reachable DataHub instance and
configure `mcp-server-datahub` for that instance before running the context
builder. The included `examples/context_bundle.json` is a captured demo result;
it is not generated from bundled test data.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set your NVIDIA API key in `.env`:

```dotenv
NVIDIA_API_KEY=your_key_here
```

Never commit `.env`; it is ignored by Git.

## Build DataHub context

With DataHub and `mcp-server-datahub` configured:

```powershell
python src/context_builder.py customers
```

This writes `examples/context_bundle.json`, including every paginated schema
field and the immediate upstream tables.

## Generate SQL

Grounded generation loads the DataHub context:

```powershell
python src/codegen.py "Describe your SQL task"
```

Blind generation gives the same model and task only the table name:

```powershell
python src/codegen.py --blind "Describe your SQL task"
```

Outputs are written to `examples/output_grounded.sql` and
`examples/output_blind.sql` respectively.

## License

Licensed under the Apache License 2.0. See [`LICENSE`](LICENSE).
