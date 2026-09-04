# ADR-0005: Cognitive Observability and Automated Acceptance Boundary

Status: Accepted

## Context

ADR-0004 defines the Cognitive V2 correctness, ownership, retention, and
default-off contracts. Before any final human-data validation, maintainers need
two narrower engineering capabilities:

- a local health snapshot that can explain where persisted cognitive state is
  unhealthy without changing it; and
- a deterministic acceptance runner that proves the V2/V2.1 lifecycle and its
  failure isolation on disposable databases.

These capabilities are diagnostics and verification infrastructure. They do
not authorize a new runtime service, product UI, telemetry pipeline, learning
strategy, or V3 behavior, and they do not establish that the intervention is
effective for real learners.

## Decision

### Scope and runtime boundary

The first implementation is sidecar tooling only. It may add local command-line
tools, deterministic scenarios, tests, reports, and a CI gate. It must not
modify `StudyStore`, Planner, answer submission, cognitive workers, production
state machines, or the database schema. It must not add a background task,
monitoring history table, dashboard, notification, or remote telemetry.

The health collector opens an explicitly supplied SQLite database through a
`file:` URI with `mode=ro` and then enables `PRAGMA query_only=ON`. It does not
call `StudyStore.open`, run migrations, create missing tables, rebuild a
projection, retry an outbox item, acquire or release a claim, change journal
mode, or repair an inconsistency. A missing or incompatible schema is reported
with a reason code and left untouched.

The acceptance runner operates only on databases created in its own temporary
directory. A migration scenario may modify a copied fixture, never its source
and never a user database. Scenarios use a fixed UTC origin, an injectable
clock, and deterministic fake extraction and evaluation; they do not sleep,
access the network, or call a real model.

### Data and privacy boundary

Health output is aggregated and diagnostic. Reports must not contain:

- a learner's raw answer or prompt;
- complete evidence text or evidence spans;
- model prompts or private model output;
- claim or lease tokens;
- random temporary paths; or
- other identifiers or timestamps that make an otherwise deterministic report
  unstable.

Errors are reduced to stable categories and reason codes. Reports may contain
bounded counts, lifecycle states, version identifiers, deterministic scenario
and step names, expected and actual state summaries, and redacted references
needed to locate an invariant failure. Neither health snapshots nor acceptance
reports are uploaded outside the local process except as CI artifacts produced
from synthetic acceptance data.

### Runtime mode and health status

Runtime mode and persisted-data health are separate dimensions:

```text
runtime.mode:  disabled | shadow | active | unknown
health.status: healthy | degraded | blocked
```

An offline database inspection cannot infer current in-memory configuration.
Unless a trustworthy, explicit configuration source is provided, it reports
`runtime.mode=unknown`. A disabled engine is a valid runtime mode, not a health
failure.

`degraded` denotes a recoverable condition such as backlog, retry activity,
projection lag, an expired lease eligible for takeover, or an overdue but
recoverable obligation. `blocked` denotes a hard invariant failure, including
an unsupported version set, incompatible component versions, duplicate active
obligations, an orphan or owner-mismatched claim, or a control/fencing conflict
that can no longer be trusted. `healthy` means no collected degraded or blocked
reason exists; it does not prove historical absence of an ownership violation.

Every non-healthy result uses a stable, machine-readable reason code. Reason
codes are part of the tool contract and are additive: consumers must tolerate
unknown future codes. Human-readable messages are explanatory only and must not
be used as CI keys.

### Health snapshot schema

The JSON snapshot has a versioned envelope:

```json
{
  "schema_version": 1,
  "runtime": {"mode": "unknown"},
  "health": {
    "status": "degraded",
    "reasons": [
      {"code": "projection_generation_lag", "severity": "degraded", "count": 1}
    ]
  },
  "queues": {},
  "projections": {},
  "outbox": {},
  "retention": {},
  "controls": {},
  "version_sets": {}
}
```

The aggregate sections cover extraction queue state and oldest age; requested
versus completed projection generation and dirty/failed projections; outbox
state, retry maximum, and redacted error categories; episode, obligation, and
claim lifecycle counts; active control counts; and observed supported or
unsupported version sets. Omitted or unavailable data is explicit and never
silently treated as zero.

Health CLI exit codes are:

| Code | Meaning |
|---:|---|
| `0` | Healthy |
| `1` | Degraded |
| `2` | Blocked |
| `3` | Invalid input, inaccessible database, or tool failure |

### Acceptance report schema

The runner writes both `cognitive-v2-acceptance.json` and
`cognitive-v2-acceptance.md`. The JSON form is authoritative for CI and has a
versioned envelope containing the profile, overall result, scenario results,
ordered step results, invariant results, and redacted failure differences.
Each step records its stable name, expected state, actual state, and pass/fail
result. The Markdown form presents the same conclusions for maintainers.

Reports exclude random paths, tokens, wall-clock timestamps, and unstable IDs.
After normalization, two runs with the same profile and inputs must produce
identical reports.

Acceptance runner exit codes are:

| Code | Meaning |
|---:|---|
| `0` | All selected scenarios and invariants passed |
| `1` | At least one scenario or invariant failed |
| `2` | Runner setup or tool failure |

CI runs the deterministic `ci` profile and uploads both reports with
`if: always()` so a failed gate remains diagnosable.

### A/B ownership proof

The acceptance runner is the only tool in this scope allowed to claim evidence
about ownership isolation. For ordinary-answer scenarios it creates Cognitive
Off and Cognitive On databases, submits the same non-cognitive inputs to both,
and compares the protected domains:

- FSRS scheduling;
- Mastery;
- course progress and scope;
- wrong-question lifecycle; and
- ordinary attempts and evaluator results.

Storage-only timestamps may be normalized; business fields may not be ignored.
Only cognitive-owned facts, evidence, projections, outbox records,
interventions, episodes, obligations, claims, and satisfactions may differ.
Any other difference is a hard invariant failure and returns a non-zero exit
code.

A point-in-time health snapshot has no write-attribution history and therefore
must not emit a fabricated `ownership_violations=0` result. It can report only
inconsistencies observable in the supplied database.

### Required acceptance coverage

The acceptance suite covers the successful
`supported -> probe -> repair -> transfer -> monitored -> retention -> resolved`
path; same-hypothesis relapse; rescheduling for another error, `partial`, or
`dont_know`; dismiss, suppress, delete, and restore; outbox retry exhaustion and
normal-answer isolation; lease takeover and stale-worker fencing; restart,
rebuild, same-time, and out-of-order facts; unknown version sets; migration on
a copied fixture; retention family and timing constraints; and complete
default-off equivalence.

Any ownership, identity, fencing, control, version, projection, episode, or
obligation hard invariant fails closed and fails the acceptance gate.

### Interpretation limit

Passing these tools establishes only that the implemented mechanics are
observable, deterministic under the covered scenarios, fail closed where
required, and preserve the declared ownership boundary. It is not evidence of
real-world pedagogical benefit, retention improvement, personalization quality,
or safety on human data. Those claims remain prohibited until a separately
approved final human validation is completed. Passing this gate does not
authorize V3 or enable any cognitive feature by default.

## Consequences

Maintainers gain a local, privacy-preserving explanation of persisted cognitive
health and a repeatable CI artifact for the end-to-end contract. The deliberate
sidecar boundary limits runtime risk and keeps production behavior unchanged.

The trade-off is that the health tool has no historical attribution and no
automatic repair capability. Operators must treat `blocked` as a diagnosis,
not permission for the tool to mutate data, and use the deterministic A/B suite
rather than a snapshot when evaluating ownership isolation.
