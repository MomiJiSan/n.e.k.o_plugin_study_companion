# ADR-0003: Cognitive Remediation Loop V2

Status: Accepted for implementation behind disabled feature gates

## Context

Cognitive Evidence Engine V1 records versioned evidence for a closed-world set
of chain-rule misconceptions. V2 must use a sufficiently supported hypothesis
to change how an already-selected topic is practised without acquiring
ownership of topic selection, mastery, review scheduling, learning-plan
progress, or the wrong-question lifecycle.

The V1 incremental projector can observe extraction completion out of attempt
order. A later attempt may be projected before an earlier failed extraction is
retried, leaving the latest snapshot inconsistent with a full rebuild. V2 also
needs explicit facts for proposed and committed interventions; a generated
question alone is not proof that remediation occurred.

## Decision

V2 is limited to the `calculus.chain_rule` concept cluster (including the
repository alias `college_chain_rule`). Only `omit_inner_derivative` may become
active initially. Other catalog hypotheses remain shadow-only until they pass
their own release gates. One intervention episode targets one hypothesis.

Extraction and projection are separate stages. Extraction writes immutable,
versioned evidence and marks a topic projection dirty. A topic projector folds
all eligible evidence, intervention events, and user controls in deterministic
fact order and atomically replaces the topic's projection. V2 deliberately
uses a full topic rebuild instead of a suffix cursor because the supported
scope is small and correctness under late evidence is the priority.

The current read model is stored separately from projection history. Active
reads are valid only when the topic's projected generation equals its requested
generation and all configured catalog, extractor, and projection versions
match. Otherwise the cognitive state is empty and the original question plan
is used.

Evidence state, intervention phase, and user override are distinct concerns:

- evidence state: `hypothesized`, `supported`, or `contradicted`;
- intervention phase: `idle`, `probing`, `remediating`,
  `provisionally_resolved`, or `monitored`;
- user override: dismissed, temporarily suppressed, or deleted.

The deterministic cognitive intent policy may decorate only
`learning_intent`, `hypothesis_target`, and `repair_strategy`. It must preserve
the selected topic, selection reason, eligible topics, plan and scope
revisions, wrong-question binding, and difficulty ownership. LLM output cannot
select a strategy, transition state, create hypothesis codes, or certify its
own diagnosticity.

V2 ends at `monitored` after a validated probe, repair, and transfer check.
Delayed retention and `resolved` belong to V2.1. Strategy personalization
belongs to V3.

## User control semantics

- `dismiss` overrides model evidence until an explicit restore;
- `suppress` is temporary and requires an expiry;
- `delete` removes derived evidence and projections while preserving the raw
  attempt and a tombstone that prevents reconstruction;
- `restore` removes the override but does not resurrect deleted evidence.

Derived cognitive data is never treated as a personality, intelligence,
attention, or learning-style assessment. Uncalibrated internal scores are not
shown as probabilities.

## Failure behavior

All V2 paths fail closed. A stale read, version mismatch, invalid intervention,
validator failure, scope change, lease loss, or exception returns the original
question plan. Cognitive work remains outside the synchronous model extraction
path, and failure must never prevent an evaluated attempt from being committed.

Any change to topic selection, learning-plan or scope revision, wrong-question
binding, FSRS due time, mastery, or wrong-question state is a release-blocking
ownership violation.

## Consequences

V2 adds a versioned extraction queue, a topic projection queue, a current-state
read model, and an append-only intervention ledger. This is more storage than
the V1 four-table design, but each table has one owner and the separation makes
late extraction, retries, rebuilds, version rollback, and intervention audit
deterministic.

All gates remain disabled by default. A local single-user rollout may be called
`ACTIVE_LOCAL` only after its personal evidence gates pass; it is not evidence
of general effectiveness.
