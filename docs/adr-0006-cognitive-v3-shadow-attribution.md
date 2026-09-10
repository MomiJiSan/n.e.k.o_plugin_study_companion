# ADR-0006: Cognitive V3 Shadow Strategy Attribution Boundary

Status: Implemented in PR #97 behind a default-off feature gate;
implementation reconciled at `433dbba` on 2026-09-10

## Context

V2.1 can deliver a reviewed probe, repair, transfer check, and delayed
retention check for the active chain-rule `omit_inner_derivative` hypothesis.
It records evaluator and lifecycle provenance, but it has no stable identity
for the teaching strategy a learner actually received and no contract for
comparing later outcomes by strategy.

V3-PR1 supplies that attribution foundation. It must preserve the ownership,
failure-isolation, user-control, version-set, and default-off boundaries in
ADR-0004. Shadow results are observational evidence and are not an online
policy input.

## Decision

### Strategy catalog

Strategies come only from an immutable, manually curated, versioned catalog.
The first catalog version is `cognitive-strategy-catalog-v1` and contains five
families:

| Family | Reviewed delivery code | Measurement purpose | Baseline |
|---|---|---|---|
| Compare correct and wrong steps | `compare_steps` | probe | no |
| Complete steps | `complete_inner_derivative` | repair | yes |
| Minimal change | `minimal_change` | repair | no |
| Structure discrimination | `structure_classification` | probe | yes |
| Alternate representation | `cross_form_transfer` | transfer | yes |

Each entry pins a stable strategy ID and version, canonical topic, hypothesis,
learning intent, measurement purpose, comparison scope, baseline flag, and one
or more existing reviewed blueprint IDs. Catalog construction fails if a
binding does not exactly match the reviewed blueprint's topic, hypothesis,
intent, and delivery strategy. A blueprint can be bound only once.

Every comparison scope contains exactly one baseline for the catalog version.
Entries are returned in a deterministic order independent of declaration
order. Models cannot add entries, select an entry, change a baseline, or
rewrite a stored strategy identity.

The first collectable scope is limited to canonical topic
`calculus.chain_rule` and hypothesis `omit_inner_derivative`. The topic alias
`college_chain_rule` may resolve to that canonical identity. Adding a catalog
entry does not authorize Planner or Coach to deliver it.

### Delivery purpose and effect attribution

The strategy family describes the reviewed teaching or measurement mechanic.
The measurement purpose separately records whether the delivered question is
a probe, repair, transfer check, or retention check. Purpose is derived from
the server-owned learning intent and must match it exactly.

Only entries with `measurement_purpose="repair"` are eligible to anchor the
primary repair-strategy effect estimate. Probe and transfer exposures remain
valuable provenance and endpoint evidence, but their strategy names cannot
turn them into repair exposures. Retention will use its own reviewed catalog
binding when one is explicitly added; V3-PR1 does not infer a strategy from an
episode or from a later outcome.

Production resolution requires the exact tuple of canonical topic,
hypothesis, learning intent, delivery strategy, and reviewed blueprint ID.
Unknown IDs and mixed tuples do not resolve and therefore cannot create a
valid strategy exposure.

### Exposure provenance

An exposure represents an actually committed question. A proposed or selected
strategy that never reaches `question_committed` creates no exposure. Strategy
identity must be resolved before commit from server-owned fields and then
frozen with catalog version, strategy version, version set, blueprint,
validator, question family, difficulty, hint state, Coach/Planner decision,
and root-fact provenance.

The immediate answer window is frozen at delivery from
`question_committed.created_at` through 24 hours later. The exposure stores
`answer_window_expires_at`; a later attempt remains in the ledger but is
excluded as late. Pending versus missing is derived from the effective time of
the report's frozen root-fact boundary, never from wall-clock report time.

`attempt_committed` may append an outcome to an existing exposure; it cannot
create or reinterpret one. Transfer and retention outcomes must follow the
certified episode and obligation provenance defined by ADR-0004. Ambiguous
repair-to-episode ancestry remains visible as an exposure but is excluded from
the primary effect estimate with a stable reason code.

Historical questions that lack a pre-commit strategy identity are not
backfilled by guessing from prompt text, final answer, current policy, or a
newer catalog. Abandoned and replaced questions remain distinct exposures.
Delete cutoffs and user controls apply to replay and prevent old facts from
being restored into a report.

Raw learner answers, reference answers, prompts, tokens, and model chain of
thought are outside the attribution ledger and report. Evaluator verdict and
its certified provenance may be consumed; V3 does not introduce another model
call or ask a model to rank strategies.

### Shadow behavior and ownership

V3 Shadow collection defaults off under an independent gate. Enabling it may
append V3-owned attribution records after normal fact commit. Failure to
resolve or persist Shadow attribution does not roll back an otherwise valid
question or answer transaction.

The catalog and offline report do not mutate FSRS scheduling, Mastery, course
scope or progress, wrong-question state, topic selection, cognitive evidence,
projection state, episodes, obligations, or evaluator verdicts. Coach remains
the sole owner of the final learning action. No report value feeds Planner or
changes the next question in V3-PR1.

Disabling the V3 gate stops new Shadow collection and report-triggered work.
Existing immutable V3 records remain available for audit and replay subject to
user deletion rules. With the gate disabled, question selection, delivery,
answer writeback, and all V2.1-owned states are behaviorally equivalent to the
existing system.

### Offline interpretation

Reports freeze `as_of_root_fact_seq`, catalog version, and complete version set.
Immediate, transfer, and retention endpoints are reported separately. The
primary comparison uses only common topic, hypothesis, reviewed comparison
family, difficulty, hint, and compatible-version strata, with one catalog
baseline per comparison scope. The original delivered-question-family grouping
was amended by [ADR-0007](adr-0007-cognitive-v3-guarded-strategy-rotation.md):
report v2 groups by the frozen `comparison_family_id` while retaining the
actual `question_family_id` for audit and independence checks. Missing historical
comparison families are excluded rather than inferred. Insufficient, missing,
pending, abandoned, replaced, late,
ambiguous, and incompatible records remain explicit.

For identical facts and version inputs, replay ordering, canonical JSON, and
the report digest are deterministic. Reports describe observed associations.
They do not establish causality, make a human-effectiveness claim, or authorize
online personalization.

## Consequences

The system gains a stable join key between reviewed delivery mechanics and
later outcomes while keeping V2.1 behavior intact. Early production data may
contain only the current baseline strategies; absent alternatives must appear
as no exposure or insufficient data rather than as synthetic evidence.

Future catalog versions may add reviewed bindings or retention strategies, but
old exposure identities and baseline declarations remain immutable. Any online
strategy selection requires a separate decision after the sample, safety, and
ownership conditions in the cognitive-engine stage document are satisfied.

## Implementation contract and reconciliation (2026-09-10)

The local checkout at `433dbba` already contains V3-PR1 (PR #97), guarded
non-personalized rotation (PR #98), comparison-family closure (PR #99), and
collection-health/runtime work (PR #100–#101). The external stage-document
snapshot dated 2026-09-07 still labels V3-PR1 `PLANNED`; that label is obsolete
for this checkout. This reconciliation documents existing implementation and
does not authorize new runtime gates, deployment, or online personalization.

### Version identities

| Identity | Current binding | Meaning |
|---|---|---|
| Engine version set | `cognitive-v2.1-1` | Existing supported engine combination in `adaptive_learning/cognitive_versions.py` |
| Engine hypothesis/blueprint catalog | `cognitive-catalog-v1` | The `catalog_version` component inside the engine version set |
| Strategy attribution catalog | `cognitive-strategy-catalog-v1` | The separate `catalog_version` frozen in strategy metadata and exposures |
| Individual strategy | Stable `chain.omit-inner.*` ID plus `v1` | Immutable reviewed delivery identity |
| Offline report | `cognitive-strategy-report-v2` | Comparison-family-aware report format and semantics |

V3-PR1 does not register an engine version set named `cognitive-v3` or replace
the engine's catalog component with the strategy catalog. Exposure identity
includes both the strategy catalog version and the complete engine
`version_set_id`; they must not be interchanged. Unknown engine version sets
disable collection through the existing configuration contract.

### Reviewed blueprint bindings

All bindings below are scoped to `calculus.chain_rule / omit_inner_derivative`.
Blueprint IDs have prefix `chain.omit-inner.`; strategy IDs have the same
prefix, and every listed strategy version is `v1`.

| Strategy ID suffix | Blueprint ID suffix | Purpose | Baseline within purpose |
|---|---|---|---|
| `compare-correct-wrong-steps` | `compare-steps.v1` | probe | no |
| `structure-discrimination` | `classify-structure.v1` | probe | yes |
| `complete-steps` | `fill-factor.v1` | repair | yes |
| `minimal-change` | `minimal-change.v1` | repair | no |
| `alternate-representation` | `cross-form-transfer.v1`, `cross-form-transfer.v2-retest` | transfer | yes |

Five catalog families therefore do not mean five competing repair strategies:
only `complete-steps` and `minimal-change` anchor repair-effect comparisons.
There is currently no reviewed retention strategy binding. Certified retention
results are appended to the linked repair exposure through its episode; the
absence of a retention strategy entry does not justify inventing an exposure.

### Source facts, derived ledger, and ownership

| Contract responsibility | Existing implementation |
|---|---|
| Resolve the exact server-owned topic/hypothesis/intent/strategy/blueprint tuple | `CognitiveStrategyCatalog.resolve_reviewed` in `adaptive_learning/cognitive_strategy_catalog.py` |
| Freeze strategy identity and the 24-hour answer window before question commit | `_v3_strategy_exposure_metadata` in `entry_tutor_question_entries.py` |
| Append a question exposure or subsequent answer/abandonment/replacement fact | `capture_cognitive_strategy_intervention` in `store_cognitive_strategy.py`, called by `insert_cognitive_intervention_event` in `store_cognitive_intervention.py` |
| Link certified transfer episodes and retention results | `capture_cognitive_strategy_episode` / `capture_cognitive_strategy_retention`, called by `store_cognitive_outbox.py` |
| Add the two V3-owned tables | `create_cognitive_strategy_schema`, called by `store_schema.py` |
| Rebuild from persisted canonical sources | `rebuild_cognitive_strategy_from_persisted_sources` in `store_cognitive_strategy.py` |
| Freeze a report input snapshot | `build_cognitive_strategy_report_snapshot` in `store_cognitive_strategy.py` |
| Calculate deterministic results / expose a read-only CLI | `adaptive_learning/cognitive_strategy_report.py` / `tools/cognitive_strategy_report.py` |

`cognitive_strategy_exposures` stores the immutable delivery identity;
`cognitive_strategy_exposure_facts` appends outcomes and lifecycle facts.
Evaluator provenance, actual hint use, response time, and episode links become
available after delivery and belong to those later facts, not retroactive
updates to the exposure row. Replaying a matching identity is idempotent;
conflicting contents are rejected. The exposure ID is exactly
`cognitive-strategy-exposure:` followed by the SHA-256 hex digest of compact
UTF-8 JSON `[question_id,strategy_id,strategy_version,catalog_version,version_set_id]`.

Canonical V2 facts remain the replay source. The V3 tables are a derived ledger:
ordinary collection is append-only, while explicit reconstruction may replace
the derived tables transactionally from those sources, respecting deletion
cutoffs. It must not rewrite the source intervention, attempt, evaluation,
episode, or satisfaction records. Reconstruction is a write operation and is
separate from the read-only report CLI.

The intervention and outbox paths isolate Shadow writes with SQLite savepoints
inside their existing transactions. "After fact commit" here means after the
canonical fact has been recorded in that transaction, not a separate durable
database commit. A failed V3 append rolls back its savepoint and preserves the
ordinary question, answer, and certified outcome writes.

`strategy_shadow_enabled=false` prevents new strategy identities on deliveries.
Already exposed questions can still receive their later audit facts; switching
collection off does not erase or sever their history. With both Shadow and
rotation disabled from the start, delivery and answer behavior follow V2.1.
The separate rotation gate and its rollback semantics are defined by ADR-0007.

### Report acceptance contract

Freeze `as_of_root_fact_seq`, strategy catalog version, and engine version set.
Derive time from that fact boundary, never the report-generation wall clock.
Report immediate, transfer, and retention endpoints separately; retention
requires the certified, independent, unhinted obligation result within
`24 hours <= interval <= 7 days` after transfer. Development time overrides,
disclosed answers, and unknown independence cannot support a natural-effect
claim.

The main estimate deduplicates eligible exposures within each learner,
hypothesis, episode, strategy, and rolling 24-hour window. Each common stratum
must contain at least 20 independent exposures for both compared strategies;
otherwise the comparison is `insufficient_data`. Output eligible and success
counts, rates, Wilson 95% intervals, fixed exposure-weighted differences, and
missing-outcome sensitivity bounds. Preserve pending, missing, abandoned,
replaced, and excluded records explicitly. Use integer/rational arithmetic for
counts and weights, six decimal places for presentation, stable ordering and
canonical JSON/digests for repeatable output. No value selects a winning
strategy in production.

### Verification recorded for this reconciliation

Two bounded runs on 2026-09-10 passed, using `uv run python -m pytest` from the
repository root:

- **62 passed**: `test_cognitive_strategy_catalog.py`,
  `test_cognitive_strategy_store.py`, `test_cognitive_strategy_report.py`,
  `test_cognitive_strategy_acceptance.py`, `test_cognitive_v3_shadow_config.py`,
  `test_cognitive_v3_shadow_entry.py`, `test_cognitive_version_sets.py`, and
  `test_cognitive_intervention_store.py` (all under `tests/`).
- **21 passed**: `test_cognitive_answer_event_integration.py`,
  `test_cognitive_strategy_rotation_runtime.py`,
  `test_cognitive_strategy_rotation_entry.py`, and `test_cognitive_outbox.py`.

These runs cover reviewed bindings, immutable/idempotent storage, deletion
cutoffs, deterministic reconstruction, report windows and sample thresholds,
default-off/unknown-version gates, production entry metadata, answer writeback
and Shadow failure isolation, and rotation fallback. The synthetic acceptance
tests use isolated databases and prove engineering behavior only. No full
suite or live learner database was run or inspected for this reconciliation;
it makes no new claim about runtime exposure counts or human effectiveness.
