# ADR-0006: Cognitive V3 Shadow Strategy Attribution Boundary

Status: Accepted for implementation behind a disabled feature gate

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
primary comparison uses only common topic, hypothesis, question-family,
difficulty, hint, and compatible-version strata, with one catalog baseline per
comparison scope. Insufficient, missing, pending, abandoned, replaced, late,
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
