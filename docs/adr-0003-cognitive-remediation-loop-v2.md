# ADR-0003: Cognitive Remediation Loop V2

Status: Accepted for implementation behind disabled feature gates

> Amendment (2026-09-03): ADR-0004 freezes the V2 correctness closure and
> V2.1 retention contract. Where this ADR describes the initial implementation
> as complete or embeds user control in the projection, ADR-0004 is
> authoritative.

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
all eligible evidence and intervention events through the single reducer and
atomically replaces the topic's projection. Root-fact sequence, late evidence,
control overlay, delete cutoff, and version composition follow ADR-0004. V2
deliberately uses a full topic rebuild instead of a suffix cursor because the
supported scope is small and correctness under late evidence is the priority.

The current read model is stored separately from projection history. Active
reads are valid only when the topic's projected generation equals its requested
generation and all configured catalog, extractor, and projection versions
match. Otherwise the cognitive state is empty and the original question plan
is used.

Evidence state, intervention phase, and current user control are distinct
concerns:

- evidence state: `hypothesized`, `supported`, or `contradicted`;
- intervention phase: `idle`, `probing`, `remediating`,
  `provisionally_resolved`, or `monitored`;
- current user control: dismissed, temporarily suppressed, or deleted, applied
  by the Reader rather than persisted as reducer truth.

The deterministic cognitive intent policy may propose only cognitive candidate
fields. Coach Planner is the sole owner of topic selection and the final plan;
the Cognitive Engine cannot decorate a plan after Planner decides it. A
candidate must preserve the selected topic, selection reason, eligible topics,
plan and scope revisions, wrong-question binding, and difficulty ownership.
LLM output cannot select a strategy, transition state, create hypothesis codes,
or certify its own diagnosticity.

V2.0 ends at `monitored` after a validated probe, repair, and transfer check.
Delayed retention and `resolved` belong to the ADR-0004 V2.1 contract.
Strategy personalization belongs to V3 and is not authorized by either ADR.

## User control semantics

- `dismiss` overrides model evidence until an explicit restore;
- `suppress` is temporary, requires an expiry, and cannot exceed 24 hours;
- `delete` removes derived evidence and projections while preserving the raw
  attempt and a permanent `delete_cutoff_seq` that prevents reconstruction;
- `restore` removes the current override but only facts created after the
  cutoff may contribute; late extraction from an older attempt stays deleted.

Derived cognitive data is never treated as a personality, intelligence,
attention, or learning-style assessment. Uncalibrated internal scores are not
shown as probabilities.

## Failure behavior

All V2 paths fail closed. A stale read, version mismatch, invalid intervention,
validator failure, scope change, lease loss, or exception returns the ordinary
question plan. Internal cognitive failure must never prevent an otherwise
valid evaluated attempt from being committed. Identity forgery or an
attempt/question/topic mismatch still rejects the request, while a storage
failure affecting the entire answer transaction fails the transaction.

Any change to topic selection, learning-plan or scope revision, wrong-question
binding, FSRS due time, mastery, or wrong-question state is a release-blocking
ownership violation.

## Consequences

V2 adds a versioned extraction queue, a topic projection queue, a current-state
read model, and an append-only intervention ledger. This is more storage than
the V1 four-table design, but each table has one owner and the separation makes
late extraction, retries, rebuilds, version rollback, and intervention audit
deterministic.

All gates remain disabled by default. The user has waived a real Shadow release
phase, not the correctness contract. Activation remains limited to
`omit_inner_derivative`, and no local result is evidence of general
effectiveness.
