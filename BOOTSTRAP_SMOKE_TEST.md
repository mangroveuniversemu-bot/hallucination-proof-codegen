# Fresh Bootstrap Smoke Test

Date: 2026-07-27

Platform: Windows, Python 3.12, Docker Desktop 29.6.2

Project commit tested: `8cd63b8d80616e674dc767558091dd5ccc5b0dde`

External dbt commit: `36bde6cba69d962b83be1d52fc65a0dce1cb4ebb`

## Verdict

**PASS with a documented host certificate remediation.** The application path
completed from an isolated clone and fresh project/dbt virtual environments:

```text
DataHub Quickstart
-> dbt build + docs generation
-> DataHub ingestion
-> MCP schema, field tags, and lineage retrieval
-> blind/grounded generation
-> schema, runtime, result/NULL, and governance gates
-> one bounded repair
-> sealed manifest
```

The orchestrator run intentionally omitted `--writeback` and reported
`Writeback: NOT_REQUESTED`.

## Verified results

| Check | Result |
| --- | --- |
| DataHub GMS and UI | HTTP 200 |
| `customers` schema | 7 fields |
| Upstream lineage | `stg_customers`, `stg_orders`, `stg_payments` |
| Field governance | `first_name` and `last_name` each read back with `editedTags: ["PII"]` |
| Blind generation | Schema FAIL; runtime FAIL |
| Grounded initial | Schema PASS; runtime PASS; result PASS; governance FAIL |
| Bounded repair | One attempt; PASS |
| Final gates | Schema, runtime, result/NULL, governance all PASS |
| Final NULL violations | 0 |
| Manifest SHA-256 | `449ec0b6a81a954aea3c98832e5a6ebb49655bba028c2582c6cbb8fd3195879b` |
| Final PowerShell replay | Exit 0; dbt 28/28 PASS; 7 fields, 3 upstreams, and both PII tags read back |

The local DataHub UI was also inspected after the run. It displayed two PII
badges and the institutional-memory addendum pointing to the canonical,
commit-pinned evidence snapshot at `40c2b6861c7f145fba8506b87fb08be8ec61abe6`.

## Host-specific observations

The first attempt exposed two Windows host conditions rather than application
failures:

1. The host PowerShell policy blocked direct `.ps1` execution. The documented
   command now uses process-scoped `-ExecutionPolicy Bypass`.
2. This host's TLS inspection root was available through the Windows trust
   store but not the old pip/Requests bundled CA. The smoke environment was
   remediated without disabling TLS verification: both fresh environments were
   upgraded offline to the `pip==26.1.2` wheel whose SHA-256
   (`382ff9f685ee3bc25864f820aa50505825f10f5458ffff07e30a6d96e5715cab`)
   is already pinned in `requirements.lock`; DataHub CLI used Python
   `truststore` for the Windows certificate store.

No `--trusted-host`, certificate-verification disablement, API key, or secret
was added to the repository. GitHub's clean Windows and Ubuntu runners install
the same hashed lock without this host-specific remediation.

## Evidence

See the [sanitized terminal transcript](examples/bootstrap_smoke_test_20260727.log).
The original full local transcript was retained outside the repository because
dependency and GraphQL debug output added noise but no additional proof.
