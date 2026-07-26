# Small Benchmark Results

Run: `20260727T120000Z`

> This is a small, controlled, directional benchmark. It does not claim that hallucinations were reduced to zero.

Verified Merge Readiness requires one generation to pass all four deterministic gates: schema, DuckDB runtime, field-level governance, and reference-result equivalence.

| Mode | Ready | VMR | Schema | Runtime | Governance | Result | Repairs |
|---|---:|---:|---:|---:|---:|---:|---:|
| Blind | 1/9 | 11.1% | 11.1% | 11.1% | 11.1% | 11.1% | 0 |
| Datahub Context | 7/9 | 77.8% | 100.0% | 100.0% | 77.8% | 77.8% | 0 |
| Context Assurance | 9/9 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 2/2 passed |

## VMR by schema world

| Mode | Familiar | Legacy | Governed |
|---|---:|---:|---:|
| Blind | 33.3% | 0.0% | 0.0% |
| Datahub Context | 100.0% | 100.0% | 33.3% |
| Context Assurance | 100.0% | 100.0% | 100.0% |

The Context + Assurance mode reuses the exact DataHub Context initial candidate. It permits at most one structured repair, so it does not gain an extra initial sample.
