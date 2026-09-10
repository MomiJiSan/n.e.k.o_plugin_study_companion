# ADR-0009: Versioned teaching coverage expansion

Status: Implemented in PR #105; 2026-09-10

## Decision

Teaching coverage follows V3 as a separately versioned V4 phase. V4-PR1
extends the reviewed chain-rule catalog from one Active error mechanism to all
three existing topic-specific mechanisms without changing the frozen
`cognitive-v2.1-1` catalog in place.

The new selectable version set is `cognitive-v4-coverage-1`. It binds:

- extractor `cognitive-extractor-v1`;
- catalog `cognitive-catalog-v2`;
- reducer `cognitive-reducer-v2.1-1`;
- projection `cognitive-v4-coverage-1`;
- validator `cognitive-question-validator-v4-coverage-1`.

The release configuration remains on `cognitive-v2.1-1`, and every cognitive
gate remains default off. Selecting the new version therefore requires an
explicit configuration change. Unknown or mismatched component versions still
disable cognitive behavior.

## First coverage batch

`cognitive-catalog-v2` preserves the existing
`calculus.chain_rule / omit_inner_derivative` behavior and activates the two
topic-specific mechanisms that were already available for Shadow extraction:

| Hypothesis | Probe | Repair | Transfer |
|---|---|---|---|
| `differentiate_inner_incorrectly` | compare the attempted inner factor | recompute the inner derivative before composing | change outer form while preserving inner-derivative accuracy |
| `confuse_product_and_chain` | classify composition versus product structure | compare composition steps with product-rule structure | transfer to a logarithmic composition |

Each cell is backed by a fixed reviewed blueprint with immutable question,
answer, family, mathematical expression, diagnostic signature and competing
hypothesis set. The model may not add a mechanism or replace protected scoring
material. Coach continues to own topic, difficulty, schedule and final
candidate acceptance.

## Compatibility boundary

- `COGNITIVE_CATALOG_V1` stays byte-for-behavior compatible: only
  `omit_inner_derivative` is Active.
- Runtime catalog selection is made from the registered version set before
  extraction, state reading, intent policy and delivery are constructed.
- Delivery and validation resolve the same catalog from the hypothesis or
  validator component version, so a V4 proposal cannot be checked against the
  V2.1 catalog accidentally.
- V3 strategy attribution, rotation and personalization remain scoped to
  `omit_inner_derivative`; the two new mechanisms always use their reviewed
  baseline repair strategy.
- V4-PR1 ends at certified transfer. Retention remains active only for
  `omit_inner_derivative` until a separate versioned retention catalog and
  transaction review are completed.
- Knowledge-graph generic hypotheses remain Shadow only and do not gain Active
  blueprints through this decision.

## Acceptance

Targeted acceptance must prove:

1. V2.1 retains exactly one Active hypothesis.
2. V4 exposes exactly three Active chain-rule hypotheses.
3. Both new mechanisms produce reviewed probe, repair and transfer questions.
4. The V4 validator accepts those exact payloads and rejects catalog/version
   mismatches through existing fail-closed behavior.
5. A runtime `KnowledgeTracker` selects catalog v2 only when configured with
   `cognitive-v4-coverage-1`.
6. Existing catalog, state-policy, delivery, projection and ordinary-question
   regressions continue to pass.

The first bounded regression passed all `479` cognitive-targeted tests. Ruff,
both configured Pyright suites and Python compilation passed. No repository-wide
test, installation, live learner database operation or human validation was
performed.

No human-effectiveness claim follows from this expansion. Installation and
human validation remain separate, and the release defaults remain inert.
