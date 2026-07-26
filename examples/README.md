# Reproducible evidence

This folder contains checked-in artifacts from three controlled evaluations.

## Schema grounding

- `output_blind.sql`: GLM invents `clv`.
- `blind_runtime_error.txt`: DuckDB rejects it and suggests the real field.
- `output_grounded.sql`: grounded GLM uses `customer_lifetime_value`.
- `grounded_runtime_output.txt`: the same task executes successfully.

## PII Governance Gate

- `output_pii_candidate.sql`: valid SQL that projects classified PII.
- `governance_fail.json`: violations for `first_name` and `last_name`.
- `governance_repair.json`: standalone deterministic AST repair.
- `governance_pass.json`: repaired output passes the same policy.

## Change admission controller

- `context_bundle.json`: DataHub schema, upstreams, downstreams, tags, and exact
  lineage paths.
- `impact_report.json`: LOW/MEDIUM/HIGH routing and final auto-merge block.
- `admission_report.json`: structured failure, one GLM repair, re-validation,
  and downstream admission decision.
- `output_agent_repaired.sql`: SQL returned by the single repair call.
- `agent_repair_runtime_output.txt`: repaired SQL executes in DuckDB.
- `impact_*_ui_evidence.png`: DataHub Impact Analysis shows the tagged demo
  consumers.

The three downstream business assets are intentionally synthetic and are
created by `src/bootstrap_impact_demo.py`; they are never presented as part of
the upstream dbt Labs project.
