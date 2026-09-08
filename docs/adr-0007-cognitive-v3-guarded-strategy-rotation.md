# ADR-0007: V3 guarded repair strategy rotation

- Status: Accepted; implemented pending merge
- Date: 2026-09-08
- Scope: `calculus.chain_rule / omit_inner_derivative / misconception_repair`

## Decision

V3 may assign one of two already reviewed repair strategies only after the
existing Active cognitive policy and Coach-owned plan have selected the same
topic and confirmed hypothesis:

- baseline: `complete_inner_derivative`;
- alternate: `minimal_change`.

Assignment is deterministic and non-personalized.  It hashes only a versioned
experiment identifier plus canonical topic, hypothesis identity and the
certified source attempt identity.  Raw assignment identities are not copied
into the rotation audit payload.  Replaying the same frozen identity always
produces the same assignment.  A permitted retry after a failed repair reuses
the latest persisted server assignment only when hypothesis, experiment,
scope, bucket, variant and delivered strategy all remain consistent.

## Required gates

Rotation is active only when all of the following are true:

1. cognitive projection is enabled;
2. read mode is `active`;
3. intent policy is `on`;
4. strategy exposure collection is enabled;
5. the independent strategy rotation switch is enabled;
6. the proposed action is the reviewed repair scope above;
7. the hypothesis has a stable hypothesis ID and certified source attempt ID.

Every switch defaults to off except the pre-existing knowledge graph switch.
An unknown version set, malformed boolean, missing identity, catalog mismatch
or scope mismatch keeps the baseline V2.1 decision unchanged.

## Ownership and safety

Rotation changes only `repair_strategy` on an already accepted cognitive
repair proposal.  It does not change topic, difficulty, question type,
practice scope, scheduling, course coverage, mastery, FSRS, wrong-question
state or retention timing.  The existing Planner/Coach merge remains the
acceptance boundary, and the reviewed blueprint validator remains mandatory.

The assignment does not inspect learner traits, compare strategy outcomes,
scores, response time or an offline report.  The prior verdict is read only to
recognize an allowed failed-repair retry and preserve its existing assignment;
it never chooses a winning variant.  Reports remain read-only and are not
connected to Planner or Coach.  This is controlled exposure collection, not
online personalization.

The selected variant and a SHA-256 assignment-key digest are appended to both
`intent_proposed` and `question_committed` metadata.  The actual delivered
blueprint continues to determine the immutable V3 strategy exposure identity.

## Rollback

Set `strategy_rotation_enabled = false`.  Exposure collection may remain on to
observe the baseline strategy.  If collection is also disabled, question and
answer behavior must remain identical to V2.1 and no new V3 exposure is
created.  Existing intervention and exposure facts are never rewritten.

## Non-goals

- no additional topic or hypothesis;
- no model-generated strategy;
- no adaptive winner selection;
- no automatic promotion from offline reports;
- no claim of causal or human teaching effectiveness;
- no remote telemetry or UI dashboard.
