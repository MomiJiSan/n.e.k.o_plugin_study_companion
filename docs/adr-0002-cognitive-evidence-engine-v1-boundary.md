# ADR-0002: Cognitive Evidence Engine V1 boundary and contracts

- Status: Accepted; PR0-PR4 implemented behind disabled-by-default gates
- Date: 2026-09-02
- Scope: `calculus.chain_rule` only

## Context

The study companion already has authoritative owners for course plans and
progress, topic mastery, FSRS review time, wrong-question lifecycle, and daily
goals. Error-mechanism evidence is useful, but treating it as another mastery
score or another coach would create conflicting state and action ownership.

V1 therefore accepts only server-evaluated, structured attempts that are bound
to an existing topic. It does not inspect free chat, ordinary explanations,
OCR documents, personality, intelligence, or learning style.

## Decision

The Cognitive Evidence Engine exclusively owns versioned, reversible
hypotheses about observable error mechanisms. It may eventually submit a
`LearningActionCandidate`, but the existing coach remains the only component
that selects the final learning action.

Decision-making stays in two phases:

1. Existing retry, FSRS, mastery, and recommendation policy selects the topic.
2. Cognitive policy may decorate that same topic with a `LearningIntent` and a
   versioned `HypothesisRef`.

The decorator must preserve the topic, learning-plan identity and revision,
scope identity and revision, wrong-question binding, and eligible-topic set.
`blocked_diagnostic` remains a prerequisite-readiness mechanism and is not
reused for misconception diagnosis.

## Contract decisions

`SelectionReason` answers why an existing topic was selected.
`LearningIntent` independently answers how that topic should be taught:
`practice`, `readiness_probe`, `misconception_probe`,
`misconception_repair`, `transfer_check`, or `retention_check`.

`QuestionPlan` appends `learning_intent="practice"` and
`hypothesis_target=None` after all existing fields. Appending rather than
inserting preserves legacy positional construction. The legacy
`misconception_target` remains model-facing descriptive text and is never a
cognitive-state key.

`HypothesisRef` identifies a topic-local hypothesis projection together with
its status, probability, and model version. `LearningActionCandidate` is an
internal shadow proposal, not an instruction to modify FSRS, mastery, progress,
or wrong-question state. `CognitiveStatePort` is deliberately read-only.

## V1 catalog and configuration

The only supported concept cluster is `calculus.chain_rule`. The repository's
existing seed ID `college_chain_rule` is a runtime alias for that same cluster;
evidence retains the attempt's real topic ID and no duplicate topic is created.
The initial catalog is limited to:

- `omit_inner_derivative`
- `differentiate_inner_incorrectly`
- `confuse_product_and_chain`

The top-level `[cognitive]` configuration exposes four independent gates:

- `projection_enabled = false`
- `read_mode = "off"`
- `intent_policy = "off"`
- `ui_enabled = false`

The fixed V1 model version is `cognitive-v1`. Unknown modes, non-boolean gate
values, unknown model versions, and unsupported topics fail closed. Merely
parsing configuration has no runtime side effect.

## Staged implementation

PR0 changes contracts only. PR1-PR4 add the queue, immutable evidence,
rebuildable snapshots, strict extraction, projection, and the shadow runtime.
Answer submission only enqueues atomically; it never calls an LLM inside the
answer transaction. The read layer, question-intent policy, and UI remain
deferred to PR5-PR8.

The future state machine must prevent a single ordinary error from advancing
beyond `hypothesized`, require independent evidence for `supported`, prioritize
user dismissal or suppression, allow resolved hypotheses to relapse, and make
incremental projection exactly reproducible by full rebuild.

## Compatibility and rollback

With all defaults unchanged, question planning still produces `practice`, no
hypothesis target exists, and the runtime neither enqueues nor reads cognitive
state. Rollback consists of setting every cognitive gate to its disabled value.
The additive tables need not be dropped.

## Consequences

This boundary avoids a second mastery system and a second coach. It also means
V1 cannot claim a complete cognitive twin: until shadow precision and rebuild
consistency meet rollout gates, the feature is named Cognitive Evidence Engine
Shadow and cannot influence topic selection.
