# ADR-0004: Cognitive V2 Correctness and Retention Contract

Status: Accepted for implementation behind disabled feature gates

## Context

ADR-0003 established the ownership boundary and the first chain-rule
remediation loop. The implementation delivered useful evidence, projection,
intervention, and UI foundations, but those foundations do not yet satisfy the
full V2 correctness contract. In particular, extraction completion order can
still affect a rebuilt projection, an older transfer event can overwrite a
newer relapse, expired suppression can remain embedded in a projection, and a
late extraction from before a delete can reappear after restore.

V2.1 also needs a durable representation of a delayed retention check. It
cannot borrow FSRS scheduling state, infer completion from a generated
question, or treat a worker lease as the user-facing eligibility window.

The user has explicitly chosen to skip a real-data Shadow release gate. This
waives that product rollout prerequisite only. It does not weaken correctness,
fail-closed, test, ownership, or default-off requirements.

## Decision

### Ownership

Each domain has exactly one owner:

| Domain | Sole owner | Cognitive Engine permission |
|---|---|---|
| Topic selection and eligible topics | Coach Planner | Submit candidates only |
| Review timing | FSRS | Supply `not_before` and `due_by` only |
| Mastery | Mastery | Read only |
| Wrong-question state | Wrong Question | Reference only |
| Course progress and scope | Learning Plan | Validate revision only |
| Hypotheses, episodes, and retention | Cognitive Engine | Own completely |
| Final answer verdict | Evaluator | Consume only |
| User control | User | Obey immediately |

The Cognitive Engine does not add an active subject or misconception beyond
the chain-rule `omit_inner_derivative` path, personalize teaching strategies,
build a general workflow engine or global event bus, expose probability or
judgements about intelligence, attention, or learning style, modify the
N.E.K.O core repository, discard a normal answer because an internal cognitive
operation failed, or let retention overwrite an FSRS schedule.

### Fact order and the single reducer

Cognitive state has three logical layers:

```text
immutable facts
    -> one deterministic reducer
       |- evidence_status
       |- intervention_stage
       `- monitoring episode
    -> Reader overlays current user control
```

A cognitive-only `cognitive_fact_roots` sequence gives every attempt, question,
and control a monotonic `root_fact_seq`. Evidence extracted later inherits the
source attempt's sequence. Evidence and the outcome for the same attempt form
one atomic fact package. The reducer folds the complete ordered fact stream;
there is no separate evidence-then-intervention precedence pass.

The old `status` remains a derived compatibility field and is never an input
to a decision. The old `user_override` column remains for additive migration
compatibility, but new projections and reads do not depend on it. Reader
applies the latest effective control at read time so suppression expiry takes
effect without a new projection.

`delete` permanently records `delete_cutoff_seq`. A later `restore` permits
only facts whose root sequence is after the cutoff. It never resurrects an old
fact, including an extraction that completes late for a pre-delete attempt.
Suppression must have an expiry no more than 24 hours after creation.

### Version set

The supported combination is registered as a `CognitiveVersionSet`, rather
than inferred from a single overloaded `model_version`. A set pins the catalog,
extractor, reducer/projection, policy, blueprint, and validator versions that
may be composed. Stored facts keep their component provenance. Readers,
workers, and question delivery accept only a registered compatible set.
Unknown sets, component mismatches, stale projections, stale episodes, and
obligation conflicts fail closed to an ordinary `QuestionPlan`.

The first frozen set is named `cognitive-v2.1-1`. New reducers write their own
versioned projection and do not overwrite the old projection, so rollback can
continue reading an older supported set.

### Planner boundary

Coach Planner is the sole selector of topics and final actions. The Cognitive
Engine emits `LearningActionCandidate` values with `due_by` and obligation
references; it never mutates a plan after Planner has decided it.
`blocked_diagnostic` remains a distinct `SelectionReason` and uses
`learning_intent="readiness_probe"`. A readiness probe cannot carry cognitive
decoration.

Wrong-question retry and overdue FSRS review remain higher priority. Before a
retention obligation's `due_by`, it may only merge opportunistically with the
same topic. After `due_by`, it may become a Coach candidate, but Coach still
decides. One question binds at most one cognitive hypothesis while it may also
satisfy FSRS or wrong-question obligations.

### Answer transaction and failure classes

The normal answer transaction atomically stores the attempt, evaluator result,
normal FSRS/Mastery/wrong-question updates, and a reference-only cognitive
outbox entry. The outbox does not copy the user's raw answer.

- Forged client provenance or question/attempt/topic identity mismatch rejects
  the request.
- An internal cognitive validation, projection, or worker failure does not
  reject or roll back an otherwise valid normal answer; the outbox records the
  failed or retryable cognitive work.
- A database or disk failure affecting the whole transaction fails the whole
  transaction.
- A stale obligation or control lets the normal answer commit and discards its
  cognitive effect.

`attempt_committed` is terminal and cannot transition to abandonment. Question
commit and attempt commit are unique and idempotent. Worker claim, takeover,
and completion use a short independent lease, CAS, and worker-identity fence.

### Episode and obligation lifecycle

A certified transfer success atomically creates a `MonitoringEpisode`. Each
open episode has at most one effective retention `LearningObligation`.
`ObligationClaim` uses a token, a short lease, and CAS. Cancellation, timeout,
or takeover releases or terminates the old claim. `dismiss`, `suppress`, and
`delete` atomically cancel or pause related obligations. A completed
obligation is independent of projection generation and cannot be regenerated
by a rebuild.

The fixed timing contract, measured from certified transfer success, is:

| Field or limit | Value |
|---|---|
| `not_before` | 24 hours |
| `due_by` | 72 hours |
| `eligibility_until` | 7 days |
| Retention frequency | At most once per hypothesis per rolling 24 hours |
| Cooldown after relapse | 24 hours |
| Worker lease | Separate short timeout; never reuse `eligibility_until` |

If no valid check completes by seven days, the episode becomes `expired`, the
intervention stage becomes `provisionally_resolved`, and a new transfer check
is scheduled through Coach candidates. Expiry is neither `resolved` nor a
relapse.

### Retention result matrix

| Result | State change |
|---|---|
| Correct, delayed, independent, unhinted, certified item | `monitored -> resolved` |
| Support evidence for the same hypothesis | `-> supported` and increment `relapse_count` |
| Error explained by a different mechanism | Stay `monitored`; reschedule within the window |
| `partial` or `dont_know` | Stay `monitored`; reschedule within the window |
| Early, repeated family, or version-incompatible result | Ordinary evidence only; do not satisfy the obligation |
| Missed `eligibility_until` | Episode `expired`; return to `provisionally_resolved`; schedule transfer again |

Only evaluator verdicts are consumed. Retention certification additionally
records whether a hint was used, evaluator kind/version/confidence, episode and
obligation identity, timing-window certification, and blueprint/validator
versions. A wrong answer is not automatically a relapse; only support evidence
for the same hypothesis is.

### Gates and release scope

All new behavior defaults off:

```ini
[cognitive]
projection_enabled = false
read_mode = "off"
intent_policy = "off"
ui_enabled = false
retention_enabled = false
version_set = "cognitive-v2.1-1"
```

V2.0 active intervention requires projection, `read_mode="active"`, and
`intent_policy="on"`. V2.1 additionally requires
`retention_enabled=true`. Disabling retention stops producing and consuming
new retention obligations. Disabling intent still records ordinary attempts
and eligible evidence but delivers no cognitive question.

The V2.0 correctness closure, monitoring episodes, obligations, and end-to-end
retention are targeted for `v0.2.6`. Even then,
the only active misconception remains `omit_inner_derivative`; all other
hypotheses remain non-active.

## Migration and rollback

Schema changes are additive. Existing v0.2.5 tables and columns remain. Delete
cutoffs are permanent and must also be respected by rollback code. New
reducers use independent version-set projections. Turning every cognitive gate
off must be behaviorally equivalent to v0.2.5 normal learning behavior, apart
from inert additive storage.

## Consequences

V2 is no longer described as fully complete merely because the initial active
path and simulated Shadow checks exist. The prior implementation is a tested
foundation; correctness closure requires deterministic root-fact ordering,
single-reducer rebuild equivalence, real-time user control, Planner ownership,
answer failure isolation, and the persistent episode/obligation model.

Skipping real Shadow removes a waiting phase, but it increases the importance
of disabled defaults, targeted fault and concurrency tests, fail-closed
fallback, reversible version sets, and a narrow active scope. This decision
does not authorize V3 or broader active hypotheses.
